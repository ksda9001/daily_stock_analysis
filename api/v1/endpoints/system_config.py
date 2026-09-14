"""System configuration endpoints."""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any, Dict, Iterable, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from api.deps import get_runtime_scheduler_service, get_system_config_service
from api.v1.schemas.common import ErrorResponse
from api.v1.schemas.system_config import (
    AgentBackendStatusPreviewRequest,
    AgentBackendStatusResponse,
    DiscoverLLMChannelModelsRequest,
    DiscoverLLMChannelModelsResponse,
    ExportSystemConfigResponse,
    GenerationBackendStatusPreviewRequest,
    GenerationBackendStatusResponse,
    ImportSystemConfigRequest,
    SystemConfigConflictResponse,
    SystemConfigResponse,
    SystemConfigSchemaResponse,
    SetupStatusResponse,
    TestGenerationBackendRequest,
    TestGenerationBackendResponse,
    SystemConfigValidationErrorResponse,
    TestLLMChannelRequest,
    TestLLMChannelResponse,
    TestNotificationChannelRequest,
    TestNotificationChannelResponse,
    UpdateSystemConfigRequest,
    UpdateSystemConfigResponse,
    ValidateSystemConfigRequest,
    ValidateSystemConfigResponse,
)
from src.auth import COOKIE_NAME, is_auth_enabled, refresh_auth_state, verify_session
from src.services.system_config_service import (
    ConfigConflictError,
    ConfigImportError,
    ConfigValidationError,
    SystemConfigService,
)
from src.services.runtime_scheduler import RuntimeSchedulerService

logger = logging.getLogger(__name__)

router = APIRouter()


_FORBIDDEN_MESSAGE = "普通成员无权访问或修改系统设置，请联系管理员"


def _multiuser_principal():
    """多用户模式下返回当前 ``Principal``；单用户模式返回 ``None``。

    返回 ``None`` 表示「不受租户角色约束」—— 与改动前 ``_assert_admin`` 的
    语义一致：单用户部署里系统设置本来就归本机用户所有。
    """
    try:
        from src.tenancy.context import current_principal, multiuser_enabled

        if not multiuser_enabled():
            return None
        return current_principal()
    except Exception as exc:  # noqa: BLE001 - 探测失败时按「放行」处理，与旧行为一致
        logger.warning("[system_config] failed to probe admin role: %s", exc)
        return None


def _is_restricted_principal(principal) -> bool:
    """该主体是否受「普通成员」限制。"""
    if principal is None:
        return False
    from src.tenancy.context import ROLE_ADMIN

    return getattr(principal, "role", None) != ROLE_ADMIN


def _forbidden_response(offending: Optional[Iterable[str]] = None) -> HTTPException:
    detail: Dict[str, Any] = {"error": "forbidden", "message": _FORBIDDEN_MESSAGE}
    if offending:
        from src.tenancy.settings import USER_SCOPED_CONFIG_KEYS

        detail["forbidden_keys"] = sorted(str(key) for key in offending)
        detail["user_scoped_keys"] = sorted(USER_SCOPED_CONFIG_KEYS)
    return HTTPException(status_code=403, detail=detail)


def _assert_admin():
    """Ensure current user is admin in multi-user mode."""
    principal = _multiuser_principal()
    if _is_restricted_principal(principal):
        raise _forbidden_response()


def _user_scope_config_version(user_id: int) -> str:
    """用户级配置的版本指纹。

    ⚠️ **不能**复用全局 ``config_version`` —— 那是整个 ``.env`` 的
    ``mtime:sha256``，把它交给普通成员等于泄露「全部密钥的哈希」。
    这里只对用户自己有权看到的那几项做指纹。
    """
    from src.tenancy.settings import USER_SCOPED_CONFIG_KEYS, effective_config_value

    parts = []
    for key in sorted(USER_SCOPED_CONFIG_KEYS):
        value, source = effective_config_value(key, user_id)
        parts.append(f"{key}={source}:{value!r}")
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"user-scope:{digest}"


def _user_scoped_config_payload(payload: Dict[str, Any], user_id: int) -> Dict[str, Any]:
    """把系统配置响应裁剪成「该用户可以自己调整的那几项」。

    ⚠️ 安全关键：这里是**按白名单重建**，不是「过滤后透传」。绝不能把
    ``service.get_config()`` 的完整结果（LLM 密钥、Webhook、邮箱授权码……）
    以任何形式交给普通成员。
    """
    from src.tenancy.settings import (
        USER_SCOPED_CONFIG_KEYS,
        effective_config_value,
        encode_value,
        get_spec,
    )

    schema_by_key = {
        item.get("key"): item
        for item in (payload.get("items") or [])
        if isinstance(item, dict)
    }

    items: List[Dict[str, Any]] = []
    for key in sorted(USER_SCOPED_CONFIG_KEYS):
        template = schema_by_key.get(key)
        if template is None:
            continue
        value, _source = effective_config_value(key, user_id)
        spec = get_spec(key)
        text = "" if value is None else (encode_value(spec, value) or "")
        item = {
            "key": key,
            "value": text,
            "raw_value_exists": value is not None,
            "is_masked": False,
        }
        if "schema" in template:
            item["schema"] = template["schema"]
        items.append(item)

    return {
        "config_version": _user_scope_config_version(user_id),
        "mask_token": payload.get("mask_token") or "******",
        "items": items,
        "llm_model_providers": [],
        "updated_at": None,
    }


def _update_user_scoped_config(request, principal):
    """普通成员的系统设置写入：只接受「用户级」键，写进该用户自己的设置。

    与 ``.env`` 完全无关，因此：

    * **不做**全局配置版本校验（用户改的是自己的偏好，跟 ``.env`` 变没变无关）；
    * **不触发**运行时重载（按用户生效是读取时叠加的，无需重载）。
    """
    from src.tenancy.settings import (
        KIND_BOOL,
        USER_SCOPED_CONFIG_KEYS,
        encode_value,
        get_spec,
        save_user_settings,
    )

    _BOOL_LITERALS = {"true", "false", "1", "0", "yes", "no", "on", "off", ""}

    offending = [
        item.key
        for item in request.items
        if (item.key or "").strip().upper() not in USER_SCOPED_CONFIG_KEYS
    ]
    if offending:
        raise _forbidden_response(offending)

    updates: Dict[str, Any] = {}
    for item in request.items:
        key = (item.key or "").strip().upper()
        spec = get_spec(key)
        if spec is None:
            raise _forbidden_response([item.key])
        if spec.kind == KIND_BOOL and str(item.value).strip().lower() not in _BOOL_LITERALS:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "validation_failed",
                    "message": "System configuration validation failed",
                    "issues": [
                        {
                            "key": key,
                            "severity": "error",
                            "message": "布尔项只接受 true/false（或 1/0、yes/no、on/off）",
                        }
                    ],
                },
            )
        updates[key] = encode_value(spec, item.value)

    written = save_user_settings(principal.user_id, updates)
    if not written:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation_failed",
                "message": "System configuration validation failed",
                "issues": [{"key": k, "severity": "error", "message": "不支持的配置项"} for k in updates],
            },
        )

    logger.info(
        "[system_config] user %s updated user-scoped config: %s",
        getattr(principal, "username", principal.user_id),
        ",".join(sorted(written)),
    )
    return UpdateSystemConfigResponse.model_validate(
        {
            "success": True,
            "config_version": _user_scope_config_version(principal.user_id),
            "applied_count": len(written),
            "skipped_masked_count": 0,
            "reload_triggered": False,
            "updated_keys": sorted(written),
            "warnings": [],
        }
    )


@router.get(
    "/scheduler/status",
    summary="Get runtime scheduler status",
    description="Return status for the in-process Web/API/Desktop scheduler.",
)
def get_scheduler_status(
    scheduler: RuntimeSchedulerService = Depends(get_runtime_scheduler_service),
) -> dict:
    """Return runtime scheduler status."""
    return scheduler.status()


@router.post(
    "/scheduler/run-now",
    summary="Run scheduled analysis now",
    description="Trigger one isolated analysis run through the current runtime scheduler service.",
)
def run_scheduler_now(
    scheduler: RuntimeSchedulerService = Depends(get_runtime_scheduler_service),
) -> dict:
    """Trigger one runtime scheduled analysis run."""
    result = scheduler.run_now()
    if not result.get("accepted", False):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "scheduler_busy",
                "message": "A scheduled analysis is already running",
                "reason": result.get("reason", "analysis_already_running"),
            },
        )
    return result


class EnvBackupAccessDenied(Exception):
    """Raised when raw `.env` backup access is not allowed for this request."""

    def __init__(self, *, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _allow_env_backup_access(request: Request) -> None:
    """Gate raw .env backup/restore to explicit secure modes.

    - Desktop runtime keeps existing local behavior via DSA_DESKTOP_MODE.
    - Non-desktop runtime must have admin auth enabled and a valid session.
    """
    if os.getenv("DSA_DESKTOP_MODE") == "true":
        return

    refresh_auth_state()
    if not is_auth_enabled():
        raise EnvBackupAccessDenied(
            status_code=403,
            message="System config backup is disabled; enable admin authentication first",
        )

    cookie_val = request.cookies.get(COOKIE_NAME)
    if cookie_val and verify_session(cookie_val):
        return

    raise EnvBackupAccessDenied(
        status_code=401,
        message="System config backup requires a valid admin session",
    )


def _raise_env_backup_access_error(exc: EnvBackupAccessDenied) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={
            "error": "env_backup_access_denied",
            "message": exc.message,
        },
    )


@router.get(
    "/config",
    response_model=SystemConfigResponse,
    responses={
        200: {"description": "Configuration loaded"},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        500: {"description": "Internal server error", "model": ErrorResponse},
    },
    summary="Get system configuration",
    description=(
        "Read current configuration and return display values. Server-masked "
        "sensitive fields may return the mask token; clients should use "
        "raw_value_exists and is_masked to interpret values."
    ),
)
def get_system_config(
    include_schema: bool = Query(True, description="Whether to include schema metadata"),
    service: SystemConfigService = Depends(get_system_config_service),
) -> SystemConfigResponse:
    """Load and return current system configuration."""
    principal = _multiuser_principal()
    try:
        payload = service.get_config(include_schema=include_schema)
        if _is_restricted_principal(principal):
            payload = _user_scoped_config_payload(payload, principal.user_id)
        return SystemConfigResponse.model_validate(payload)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to load system configuration: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Failed to load system configuration",
            },
        )


@router.get(
    "/config/setup/status",
    response_model=SetupStatusResponse,
    responses={
        200: {"description": "Setup status loaded"},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        500: {"description": "Internal server error", "model": ErrorResponse},
    },
    summary="Get first-run setup status",
    description="Read a side-effect-free setup readiness summary from saved and runtime configuration.",
)
def get_setup_status(
    service: SystemConfigService = Depends(get_system_config_service),
) -> SetupStatusResponse:
    """Return first-run setup status without writing config or reloading runtime state."""
    try:
        payload = service.get_setup_status()
        return SetupStatusResponse.model_validate(payload)
    except Exception as exc:
        logger.error("Failed to load setup status: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Failed to load setup status",
            },
        )


@router.get(
    "/config/generation-backends/status",
    response_model=GenerationBackendStatusResponse,
    responses={
        200: {"description": "Generation backend status loaded"},
        500: {"description": "Internal server error", "model": ErrorResponse},
    },
    summary="Get generation backend status",
    description=(
        "Read a side-effect-free generation backend cheap-check status from "
        "saved and runtime configuration. This endpoint does not run a model request."
    ),
)
def get_generation_backend_status(
    service: SystemConfigService = Depends(get_system_config_service),
) -> GenerationBackendStatusResponse:
    """Return saved/runtime generation backend status without writing config."""
    try:
        payload = service.get_generation_backend_status()
        return GenerationBackendStatusResponse.model_validate(payload)
    except Exception as exc:
        logger.error("Failed to load generation backend status: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Failed to load generation backend status",
            },
        )


@router.post(
    "/config/generation-backends/status/preview",
    response_model=GenerationBackendStatusResponse,
    responses={
        200: {"description": "Generation backend status preview loaded"},
        400: {"description": "Validation failed", "model": SystemConfigValidationErrorResponse},
        500: {"description": "Internal server error", "model": ErrorResponse},
    },
    summary="Preview generation backend status",
    description="Run a side-effect-free cheap check against unsaved settings draft values.",
)
def preview_generation_backend_status(
    request: GenerationBackendStatusPreviewRequest,
    service: SystemConfigService = Depends(get_system_config_service),
) -> GenerationBackendStatusResponse:
    """Return generation backend status for unsaved draft values."""
    try:
        payload = service.preview_generation_backend_status(
            items=[item.model_dump() for item in request.items],
            mask_token=request.mask_token,
        )
        return GenerationBackendStatusResponse.model_validate(payload)
    except ConfigValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation_failed",
                "message": "System configuration validation failed",
                "issues": exc.issues,
            },
        )
    except Exception as exc:
        logger.error("Failed to preview generation backend status: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Failed to preview generation backend status",
            },
        )


@router.post(
    "/config/generation-backends/smoke-test",
    response_model=TestGenerationBackendResponse,
    responses={
        200: {"description": "Generation backend smoke test completed"},
        400: {"description": "Validation failed", "model": SystemConfigValidationErrorResponse},
        422: {"description": "Invalid smoke test request", "model": ErrorResponse},
        500: {"description": "Internal server error", "model": ErrorResponse},
    },
    summary="Smoke test generation backend",
    description="Run an explicit fixed-prompt generation backend smoke test without persisting config.",
)
def test_generation_backend(
    request: TestGenerationBackendRequest,
    service: SystemConfigService = Depends(get_system_config_service),
) -> TestGenerationBackendResponse:
    """Run a fixed generation backend smoke test."""
    # ⚠️ 会真的发起一次模型请求，必须限管理员。
    _assert_admin()
    try:
        payload = service.test_generation_backend(
            backend_id=request.backend_id,
            mode=request.mode,
            items=[item.model_dump() for item in request.items],
            mask_token=request.mask_token,
            timeout_seconds=request.timeout_seconds,
        )
        return TestGenerationBackendResponse.model_validate(payload)
    except ConfigValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation_failed",
                "message": "System configuration validation failed",
                "issues": exc.issues,
            },
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "validation_error",
                "message": str(exc),
            },
        )
    except Exception as exc:
        logger.error("Failed to smoke test generation backend: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Failed to smoke test generation backend",
            },
        )


@router.get(
    "/config/agent-backends/status",
    response_model=AgentBackendStatusResponse,
    summary="Get Agent Chat backend status",
    description="Read selected Agent Chat backend configuration and command capability without a model request.",
)
def get_agent_backend_status(
    service: SystemConfigService = Depends(get_system_config_service),
) -> AgentBackendStatusResponse:
    try:
        return AgentBackendStatusResponse.model_validate(service.get_agent_backend_status())
    except Exception as exc:
        logger.error("Failed to load Agent backend status: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": "Failed to load Agent backend status"},
        )


@router.post(
    "/config/agent-backends/status/preview",
    response_model=AgentBackendStatusResponse,
    summary="Preview Agent Chat backend status",
    description="Run a side-effect-free cheap check against unsaved Agent settings.",
)
def preview_agent_backend_status(
    request: AgentBackendStatusPreviewRequest,
    service: SystemConfigService = Depends(get_system_config_service),
) -> AgentBackendStatusResponse:
    try:
        return AgentBackendStatusResponse.model_validate(
            service.preview_agent_backend_status(
                items=[item.model_dump() for item in request.items],
                mask_token=request.mask_token,
            )
        )
    except Exception as exc:
        logger.error("Failed to preview Agent backend status: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": "Failed to preview Agent backend status"},
        )


@router.put(
    "/config",
    response_model=UpdateSystemConfigResponse,
    responses={
        200: {"description": "Configuration updated"},
        400: {"description": "Validation failed", "model": SystemConfigValidationErrorResponse},
        409: {"description": "Version conflict", "model": SystemConfigConflictResponse},
        500: {"description": "Internal server error", "model": ErrorResponse},
    },
    summary="Update system configuration",
    description="Update key-value pairs in .env. Mask token preserves existing secret values.",
)
def update_system_config(
    request: UpdateSystemConfigRequest,
    service: SystemConfigService = Depends(get_system_config_service),
) -> UpdateSystemConfigResponse:
    """Validate and persist system configuration updates."""
    principal = _multiuser_principal()
    # 普通成员只能改「用户级」配置，且写入的是自己的偏好而不是 `.env`。
    if _is_restricted_principal(principal):
        return _update_user_scoped_config(request, principal)
    try:
        payload = service.update(
            config_version=request.config_version,
            items=[item.model_dump() for item in request.items],
            mask_token=request.mask_token,
            reload_now=request.reload_now,
        )
        return UpdateSystemConfigResponse.model_validate(payload)
    except ConfigValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation_failed",
                "message": "System configuration validation failed",
                "issues": exc.issues,
            },
        )
    except ConfigConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "config_version_conflict",
                "message": "Configuration has changed, please reload and retry",
                "current_config_version": exc.current_version,
            },
        )
    except Exception as exc:
        logger.error("Failed to update system configuration: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Failed to update system configuration",
            },
        )


@router.get(
    "/config/export",
    response_model=ExportSystemConfigResponse,
    responses={
        200: {"description": "Env exported"},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Env backup disabled", "model": ErrorResponse},
        500: {"description": "Internal server error", "model": ErrorResponse},
    },
    summary="Export env backup",
    description="Return the raw saved .env content for configuration backup.",
)
def export_system_config(
    request: Request,
    service: SystemConfigService = Depends(get_system_config_service),
) -> ExportSystemConfigResponse:
    """Export the active `.env` file for config backup."""
    try:
        _allow_env_backup_access(request)
    except EnvBackupAccessDenied as exc:
        logger.warning("System config export blocked: %s", exc)
        _raise_env_backup_access_error(exc)

    try:
        payload = service.export_env()
        return ExportSystemConfigResponse.model_validate(payload)
    except Exception as exc:
        logger.error("Failed to export system configuration: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Failed to export system configuration",
            },
        )


@router.post(
    "/config/import",
    response_model=UpdateSystemConfigResponse,
    responses={
        200: {"description": "Env imported"},
        400: {
            "description": "Import failed",
            "content": {
                "application/json": {
                    "schema": {
                        "anyOf": [
                            {"$ref": "#/components/schemas/ErrorResponse"},
                            {"$ref": "#/components/schemas/SystemConfigValidationErrorResponse"},
                        ]
                    }
                }
            },
        },
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Env backup disabled", "model": ErrorResponse},
        409: {"description": "Version conflict", "model": SystemConfigConflictResponse},
        500: {"description": "Internal server error", "model": ErrorResponse},
    },
    summary="Import env backup",
    description="Merge raw .env text into the saved configuration with config version conflict protection.",
)
def import_system_config(
    request: ImportSystemConfigRequest,
    request_obj: Request,
    service: SystemConfigService = Depends(get_system_config_service),
) -> UpdateSystemConfigResponse:
    """Import a `.env` backup into the active config."""
    try:
        _allow_env_backup_access(request_obj)
    except EnvBackupAccessDenied as exc:
        logger.warning("System config import blocked: %s", exc)
        _raise_env_backup_access_error(exc)

    try:
        payload = service.import_env(
            config_version=request.config_version,
            content=request.content,
            reload_now=request.reload_now,
        )
        return UpdateSystemConfigResponse.model_validate(payload)
    except ConfigImportError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_import_file",
                "message": exc.message,
            },
        )
    except ConfigValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation_failed",
                "message": "System configuration validation failed",
                "issues": exc.issues,
            },
        )
    except ConfigConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "config_version_conflict",
                "message": "Configuration has changed, please reload and retry",
                "current_config_version": exc.current_version,
            },
        )
    except Exception as exc:
        logger.error("Failed to import system configuration: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Failed to import system configuration",
            },
        )


@router.post(
    "/config/validate",
    response_model=ValidateSystemConfigResponse,
    responses={
        200: {"description": "Validation completed"},
        500: {"description": "Internal server error", "model": ErrorResponse},
    },
    summary="Validate system configuration",
    description="Validate submitted configuration values without writing to .env.",
)
def validate_system_config(
    request: ValidateSystemConfigRequest,
    service: SystemConfigService = Depends(get_system_config_service),
) -> ValidateSystemConfigResponse:
    """Run pre-save validation only."""
    try:
        payload = service.validate(items=[item.model_dump() for item in request.items])
        return ValidateSystemConfigResponse.model_validate(payload)
    except Exception as exc:
        logger.error("Failed to validate system configuration: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Failed to validate system configuration",
            },
        )


@router.post(
    "/config/llm/test-channel",
    response_model=TestLLMChannelResponse,
    responses={
        200: {"description": "Channel test completed"},
        500: {"description": "Internal server error", "model": ErrorResponse},
    },
    summary="Test one LLM channel",
    description="Run a minimal LLM request against one unsaved or saved channel definition.",
)
def test_llm_channel(
    request: TestLLMChannelRequest,
    service: SystemConfigService = Depends(get_system_config_service),
) -> TestLLMChannelResponse:
    """Validate and test one channel definition without writing `.env`."""
    # ⚠️ 必须限管理员：``use_saved_secret`` 会让服务端拿**已保存的**密钥去发请求，
    # 普通成员若能调用，等于可以借用管理员的 LLM 额度。
    _assert_admin()
    try:
        payload = service.test_llm_channel(
            name=request.name,
            protocol=request.protocol,
            api_surface=request.api_surface,
            base_url=request.base_url,
            api_key=request.api_key,
            models=request.models,
            enabled=request.enabled,
            timeout_seconds=request.timeout_seconds,
            capability_checks=request.capability_checks,
            use_saved_secret=request.use_saved_secret,
        )
        return TestLLMChannelResponse.model_validate(payload)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "validation_error",
                "message": str(exc),
            },
        )
    except Exception as exc:
        logger.error("Failed to test LLM channel: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Failed to test LLM channel",
            },
        )


@router.post(
    "/config/notification/test-channel",
    response_model=TestNotificationChannelResponse,
    responses={
        200: {"description": "Notification channel test completed"},
        500: {"description": "Internal server error", "model": ErrorResponse},
    },
    summary="Test one notification channel",
    description="Send a short test notification using unsaved or saved notification configuration.",
)
def test_notification_channel(
    request: TestNotificationChannelRequest,
    service: SystemConfigService = Depends(get_system_config_service),
) -> TestNotificationChannelResponse:
    """Validate and test one notification channel without writing `.env`."""
    # ⚠️ 同 ``test_llm_channel``：``use_saved_secret`` 会动用已保存的推送密钥。
    _assert_admin()
    try:
        payload = service.test_notification_channel(
            channel=request.channel,
            items=[item.model_dump() for item in request.items],
            mask_token=request.mask_token,
            title=request.title,
            content=request.content,
            timeout_seconds=request.timeout_seconds,
        )
        return TestNotificationChannelResponse.model_validate(payload)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "validation_error",
                "message": str(exc),
            },
        )
    except Exception as exc:
        logger.error("Failed to test notification channel: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Failed to test notification channel",
            },
        )


@router.post(
    "/config/llm/discover-models",
    response_model=DiscoverLLMChannelModelsResponse,
    responses={
        200: {"description": "Model discovery completed"},
        500: {"description": "Internal server error", "model": ErrorResponse},
    },
    summary="Discover models for one LLM channel",
    description="Call one unsaved or saved channel's `/models` endpoint and return discovered model IDs.",
)
def discover_llm_channel_models(
    request: DiscoverLLMChannelModelsRequest,
    service: SystemConfigService = Depends(get_system_config_service),
) -> DiscoverLLMChannelModelsResponse:
    """Discover models for one channel definition without writing `.env`."""
    # ⚠️ 同 ``test_llm_channel``：``use_saved_secret`` 会动用已保存的密钥。
    _assert_admin()
    try:
        payload = service.discover_llm_channel_models(
            name=request.name,
            protocol=request.protocol,
            base_url=request.base_url,
            api_key=request.api_key,
            models=request.models,
            timeout_seconds=request.timeout_seconds,
            use_saved_secret=request.use_saved_secret,
        )
        return DiscoverLLMChannelModelsResponse.model_validate(payload)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "validation_error",
                "message": str(exc),
            },
        )
    except Exception as exc:
        logger.error("Failed to discover LLM channel models: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Failed to discover LLM channel models",
            },
        )


@router.get(
    "/config/schema",
    response_model=SystemConfigSchemaResponse,
    responses={
        200: {"description": "Schema loaded"},
        500: {"description": "Internal server error", "model": ErrorResponse},
    },
    summary="Get system configuration schema",
    description="Return categorized field metadata used for dynamic settings form rendering.",
)
def get_system_config_schema(
    service: SystemConfigService = Depends(get_system_config_service),
) -> SystemConfigSchemaResponse:
    """Return schema metadata for system configuration fields."""
    try:
        payload = service.get_schema()
        return SystemConfigSchemaResponse.model_validate(payload)
    except Exception as exc:
        logger.error("Failed to load system configuration schema: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Failed to load system configuration schema",
            },
        )
