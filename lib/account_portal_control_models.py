#!/usr/bin/env python3
"""门户模型发布、公开路由、免费资源与用户模型权限策略。"""

from __future__ import annotations

from account_portal_common import *
from account_portal_database import *
from account_portal_gateway import *


class PortalModelControlMixin:
    """门户模型发布、公开路由、免费资源与用户模型权限策略。该类型只提供领域方法，运行状态由组合控制器持有。"""

    @staticmethod
    def _sqlite_online_backup(source: pathlib.Path, destination: pathlib.Path) -> None:
        """在源库可能仍在线时创建可独立恢复的 SQLite 快照。"""
        if not source.is_file():
            raise RuntimeError(f"SQLite source does not exist: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(source), timeout=30) as source_db:
            with sqlite3.connect(str(destination), timeout=30) as backup_db:
                source_db.backup(backup_db)
                result = backup_db.execute("PRAGMA quick_check").fetchone()
                if not result or result[0] != "ok":
                    raise RuntimeError(f"SQLite backup quick_check failed: {destination}")
        os.chmod(destination, 0o600)

    def _migration_backup_directory(self) -> pathlib.Path:
        configured = os.environ.get("LLMCTL_BACKUP_ROOT", "").strip()
        production = str(self.config.db_path).startswith("/var/lib/llm-cluster/")
        root = pathlib.Path(
            configured or ("/var/backups/llmctl" if production else self.config.db_path.parent / "backups")
        )
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        candidates = sorted(
            (path for path in root.glob("control-plane-*") if path.is_dir()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if candidates and time.time() - candidates[0].stat().st_mtime <= PUBLIC_COMBO_BACKUP_MAX_AGE_SECONDS:
            return candidates[0]
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = root / f"routing-migration-{timestamp}"
        destination.mkdir(mode=0o700)
        return destination

    def prepare_public_combo_migration_backup(self) -> pathlib.Path | None:
        """在当前路由迁移前同时快照两个 SQLite 控制库。"""
        if self.public_combo_backup_dir is not None:
            return self.public_combo_backup_dir
        if self.db.settings().get(PUBLIC_COMBO_MIGRATION_SETTING) == PUBLIC_COMBO_MIGRATION_NAME:
            return None
        with self.db.connect() as connection:
            legacy_rows = self.rows(
                connection.execute(
                    "SELECT id,public_model_id,source_ref,source_model,mapping_kind,"
                    "mapping_id,status FROM published_models WHERE source_kind='combo' "
                    "ORDER BY created_at,id"
                ).fetchall()
            )
        if not legacy_rows:
            return None

        backup_dir = self._migration_backup_directory()
        data_dir = backup_dir / "runtime-data"
        manifest_path = data_dir / "runtime-data.json"
        if manifest_path.is_file():
            try:
                existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                existing_manifest = {}
            if existing_manifest.get("migration") == PUBLIC_COMBO_MIGRATION_NAME:
                self.public_combo_backup_dir = backup_dir
                return backup_dir
            # 即使处于控制面升级验收窗口，也不覆盖另一迁移的回滚快照。
            timestamp = datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y%m%dT%H%M%SZ"
            )
            backup_dir = backup_dir.parent / f"routing-migration-{timestamp}"
            suffix = 0
            while backup_dir.exists():
                suffix += 1
                backup_dir = backup_dir.parent / f"routing-migration-{timestamp}-{suffix}"
            backup_dir.mkdir(mode=0o700)
            data_dir = backup_dir / "runtime-data"
            manifest_path = data_dir / "runtime-data.json"
        data_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(data_dir, 0o700)
        entries: list[dict[str, Any]] = []

        portal_backup = data_dir / "portal" / self.config.db_path.name
        self._sqlite_online_backup(self.config.db_path, portal_backup)
        entries.append(
            {
                "role": "account-portal",
                "source": str(self.config.db_path),
                "backup": str(portal_backup.relative_to(backup_dir)),
                "mode": oct(self.config.db_path.stat().st_mode & 0o777),
            }
        )

        gateway_dir = self.config.db_path.parent.parent / "gateway"
        gateway_databases = sorted(
            {
                path
                for pattern in ("*.sqlite", "*.db")
                for path in gateway_dir.glob(pattern)
                if path.is_file()
            }
        )
        production = str(self.config.db_path).startswith("/var/lib/llm-cluster/")
        if production and not gateway_databases:
            raise RuntimeError(f"OmniRoute SQLite database not found under {gateway_dir}")
        for source in gateway_databases:
            backup = data_dir / "gateway" / source.name
            self._sqlite_online_backup(source, backup)
            entries.append(
                {
                    "role": "omniroute",
                    "source": str(source),
                    "backup": str(backup.relative_to(backup_dir)),
                    "mode": oct(source.stat().st_mode & 0o777),
                }
            )

        temporary = manifest_path.with_suffix(".json.new")
        temporary.write_text(
            json.dumps(
                {
                    "version": 1,
                    "migration": PUBLIC_COMBO_MIGRATION_NAME,
                    "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "databases": entries,
                    "legacy_routes": legacy_rows,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(manifest_path)
        self.public_combo_backup_dir = backup_dir
        print(
            f"[account-portal] pre-migration SQLite snapshot: {backup_dir}",
            flush=True,
        )
        return backup_dir

    @staticmethod
    def rows(rows: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in rows]

    @staticmethod
    def _qualified_target(value: str, provider: str = "") -> tuple[str, str]:
        value, provider = str(value).strip(), str(provider).strip()
        if provider and value.startswith(provider + "/"):
            value = value[len(provider) + 1 :]
        if not provider and "/" in value:
            provider, value = value.split("/", 1)
        return provider, value

    def _resolve_model_targets(self, payload: dict[str, Any]) -> list[dict[str, str]]:
        source_kind = str(payload.get("source_kind", "model"))
        source_ref = str(payload.get("source_ref", ""))
        source_provider = str(payload.get("source_provider", ""))
        source_model = str(payload.get("source_model", ""))
        if source_kind != "combo":
            provider, model = self._qualified_target(source_model, source_provider)
            return [{"provider": provider, "model": model}] if model else []

        combos = self.omni.combos()
        by_id = {str(item.get("id", "")): item for item in combos}
        by_name = {str(item.get("name", "")): item for item in combos}
        selected = by_id.get(source_ref) or by_name.get(source_model)
        if not selected:
            return []
        targets: list[dict[str, str]] = []
        visiting: set[str] = set()

        def visit(combo: dict[str, Any]) -> None:
            combo_id = str(combo.get("id", combo.get("name", "")))
            if combo_id in visiting:
                return
            visiting.add(combo_id)
            members = combo.get("models", combo.get("targets", []))
            if not isinstance(members, list):
                return
            for member in members:
                if isinstance(member, str):
                    provider, model = self._qualified_target(member)
                elif isinstance(member, dict):
                    kind = str(member.get("kind", ""))
                    if kind in {"combo", "combo-ref", "combo_ref"}:
                        nested_id = str(
                            member.get(
                                "comboName",
                                member.get(
                                    "comboId", member.get("combo_id", member.get("id", ""))
                                ),
                            )
                        )
                        nested = by_id.get(nested_id) or by_name.get(nested_id)
                        if nested:
                            visit(nested)
                        continue
                    provider = str(member.get("provider", member.get("providerId", "")))
                    model_value = str(
                        member.get(
                            "model",
                            member.get(
                                "modelId", member.get("qualifiedModel", member.get("id", ""))
                            ),
                        )
                    )
                    provider, model = self._qualified_target(model_value, provider)
                else:
                    continue
                if model and {"provider": provider, "model": model} not in targets:
                    targets.append({"provider": provider, "model": model})

        visit(selected)
        return targets

    def _managed_runtime_contexts(self) -> dict[str, int]:
        """按 vLLM 服务模型名返回当前受管部署的有效上下文。

        模型部署注册表直接生成 Worker 启动参数，因此它比接入层中可能残留的
       手工覆盖值更接近实际运行状态。控制服务不可用时返回空映射，让第三方
        模型继续使用网关原生元数据。
        """

        if self.models is None:
            return {}
        try:
            snapshot = self.models.snapshot()
        except Exception:
            return {}
        deployments = snapshot.get("registry", {}).get("deployments", {})
        result: dict[str, int] = {}
        for deployment in deployments.values():
            if not isinstance(deployment, dict) or not deployment.get("enabled", True):
                continue
            served_model = str(deployment.get("served_model_name", "")).strip()
            runtime = deployment.get("runtime", {})
            try:
                max_model_len = int(runtime.get("max_model_len"))
            except (AttributeError, TypeError, ValueError):
                continue
            if served_model and max_model_len > 0:
                result[served_model] = max_model_len
        return result

    def inspect_model(self, payload: dict[str, Any]) -> dict[str, Any]:
        """读取实际目标元数据，并采用受管 Worker 的有效上下文与输出上限。

        参数：
            payload: 已发布模型或待编辑来源的标识、类型和底层模型信息。

        返回：
            各底层目标的原生能力，以及路由组合可以安全公开的保守值。

        异常：
            ValueError: 模型不存在，或接入层无法解析任何底层目标。
        """
        if payload.get("id") and not payload.get("source_model"):
            with self.db.connect() as connection:
                row = connection.execute(
                    "SELECT * FROM published_models WHERE id=?", (str(payload["id"]),)
                ).fetchone()
            if not row:
                raise ValueError("model not found")
            payload = dict(row)
        targets = self._resolve_model_targets(payload)
        if not targets:
            raise ValueError("无法从当前接入层解析底层模型，请检查来源模型或路由组合")

        options = self.omni.combo_builder_options()
        providers = options.get("providers", []) if isinstance(options, dict) else []
        lookup: dict[tuple[str, str], dict[str, Any]] = {}
        if isinstance(providers, list):
            for provider_entry in providers:
                if not isinstance(provider_entry, dict):
                    continue
                provider_id = str(
                    provider_entry.get("providerId", provider_entry.get("id", ""))
                )
                for model_entry in provider_entry.get("models", []) or []:
                    if not isinstance(model_entry, dict):
                        continue
                    model_id = str(model_entry.get("id", model_entry.get("model", "")))
                    qualified = str(model_entry.get("qualifiedModel", ""))
                    q_provider, q_model = self._qualified_target(qualified, provider_id)
                    lookup[(q_provider or provider_id, q_model or model_id)] = model_entry

        managed_contexts = self._managed_runtime_contexts()
        managed_output_limit = max_output_tokens_limit()
        enriched: list[dict[str, Any]] = []
        capabilities: set[str] = set()
        corrected_contexts = 0
        for target in targets:
            provider, model_id = target["provider"], target["model"]
            option = lookup.get((provider, model_id), {})
            qualified = f"{provider}/{model_id}" if provider else model_id
            alias: dict[str, Any] = {}
            with contextlib.suppress(RuntimeError):
                alias = self.omni.alias_metadata(qualified)
            resolved = alias.get("resolved", {}) if isinstance(alias, dict) else {}
            if isinstance(resolved, dict):
                provider = str(
                    resolved.get("provider", resolved.get("providerAlias", provider))
                )
                model_id = str(resolved.get("model", model_id))
                qualified = str(
                    resolved.get(
                        "qualifiedId", f"{provider}/{model_id}" if provider else model_id
                    )
                )
            native = alias.get("metadata", {}) if isinstance(alias.get("metadata"), dict) else {}
            limits = native.get("limits", {}) if isinstance(native.get("limits"), dict) else {}
            native_caps = (
                native.get("capabilities", {})
                if isinstance(native.get("capabilities"), dict)
                else {}
            )
            context_window = option.get(
                "contextLength", limits.get("contextWindow", limits.get("maxInputTokens"))
            )
            max_output = option.get("outputTokenLimit", limits.get("maxOutputTokens"))
            gateway_context = int(context_window) if context_window else None
            gateway_max_output = int(max_output) if max_output else None
            managed_context = managed_contexts.get(model_id)
            if managed_context is not None:
                context_window = managed_context
                # 受管 Worker 已在 vLLM 层执行同一硬上限。门户必须展示实际可用值，
                # 不能继续把接入层残留的 64K/256K 元数据当成真实输出能力。
                max_output = min(
                    gateway_max_output or managed_output_limit,
                    managed_output_limit,
                    managed_context,
                )
                if gateway_context != managed_context:
                    corrected_contexts += 1
            for key, portal_cap in {
                "vision": "vision",
                "toolCalling": "tools",
                "supportsTools": "tools",
                "reasoning": "reasoning",
                "supportsThinking": "reasoning",
            }.items():
                if native_caps.get(key):
                    capabilities.add(portal_cap)
            capabilities.add("chat")
            enriched.append(
                {
                    "provider": provider,
                    "model": model_id,
                    "qualified_id": qualified,
                    "context_window_tokens": int(context_window) if context_window else None,
                    "gateway_context_window_tokens": gateway_context,
                    "gateway_max_output_tokens": gateway_max_output,
                    "managed_runtime_context": managed_context is not None,
                    "max_output_tokens": int(max_output) if max_output else None,
                    "family": str(
                        (native.get("metadata") or {}).get("family", "")
                        if isinstance(native.get("metadata"), dict)
                        else ""
                    ),
                    "modalities": native.get("modalities", {}),
                    "capabilities": native_caps,
                }
            )
        contexts = [item["context_window_tokens"] for item in enriched if item["context_window_tokens"]]
        outputs = [item["max_output_tokens"] for item in enriched if item["max_output_tokens"]]
        return {
            "targets": enriched,
            "target_count": len(enriched),
            "context_known_count": len(contexts),
            "output_known_count": len(outputs),
            "context_window_tokens": min(contexts) if contexts else None,
            "max_output_tokens": min(outputs) if outputs else None,
            "capabilities": sorted(capabilities),
            "native_sync_supported": all(item["provider"] for item in enriched),
            "managed_runtime_count": sum(
                1 for item in enriched if item["managed_runtime_context"]
            ),
            "managed_runtime_corrected_count": corrected_contexts,
            "read_at": now(),
        }

    def _sync_model_limits(
        self,
        metadata: dict[str, Any],
        context_window: int | None,
        max_output: int | None,
        sync_context: bool,
        sync_output: bool,
    ) -> tuple[str, str]:
        errors: list[str] = []
        attempted = 0
        for target in metadata.get("targets", []):
            provider, model = str(target.get("provider", "")), str(target.get("model", ""))
            if not provider or not model:
                if sync_context or sync_output:
                    errors.append(f"{target.get('qualified_id', model)} 缺少供应商标识")
                continue
            if sync_context and context_window:
                attempted += 1
                try:
                    self.omni.set_context_window_override(provider, model, context_window)
                except Exception as error:
                    errors.append(f"{provider}/{model} 上下文：{error}")
            if sync_output and max_output:
                attempted += 1
                try:
                    self.omni.set_max_output_override(provider, model, max_output)
                except Exception as error:
                    errors.append(f"{provider}/{model} 最大输出：{error}")
        if not attempted and not errors:
            return "read", ""
        if errors and attempted > len(errors):
            return "partial", "; ".join(errors)[:2000]
        if errors:
            return "failed", "; ".join(errors)[:2000]
        return "synced", ""

    @staticmethod
    def _registry_managed_models() -> list[tuple[str, list[str]]]:
        """读取已启用部署的公开模型及真实能力；旧安装返回空列表。"""

        registry_path = pathlib.Path(
            os.environ.get(
                "LLM_DEPLOYMENT_REGISTRY", "/etc/llm-cluster/deployments.json"
            )
        )
        try:
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        deployments = registry.get("deployments", {})
        if not isinstance(deployments, dict):
            return []
        result: list[tuple[str, list[str]]] = []
        for deployment in deployments.values():
            if (
                not isinstance(deployment, dict)
                or not deployment.get("enabled", True)
                or not deployment.get("publish_requested", True)
            ):
                continue
            runtime = deployment.get("runtime", {})
            if not isinstance(runtime, dict):
                runtime = {}
            capabilities = ["chat"]
            for enabled, capability in (
                (runtime.get("supports_image_input"), "vision"),
                (runtime.get("supports_ocr"), "ocr"),
                (runtime.get("supports_tool_calling"), "tools"),
                (runtime.get("supports_reasoning"), "reasoning"),
            ):
                if enabled:
                    capabilities.append(capability)
            public_ids = deployment.get("public_model_ids", [])
            if not isinstance(public_ids, list) or not public_ids:
                public_ids = [deployment.get("served_model_name", "")]
            for public_id in public_ids:
                model_name = str(public_id).strip()
                if model_name and (model_name, capabilities) not in result:
                    result.append((model_name, capabilities))
        return result

    def seed_managed_model(self) -> None:
        """把部署注册表当前公开模型投影到门户，并停用已退役的自动种子。"""

        managed_models = self._registry_managed_models()
        registry_owned = bool(managed_models)
        if not managed_models:
            model_name = os.environ.get("SERVED_MODEL_NAME", "").strip()
            if not model_name:
                return
            capabilities = ["chat"]
            if env_bool("SUPPORTS_IMAGE_INPUT"):
                capabilities.append("vision")
            if env_bool("SUPPORTS_OCR"):
                capabilities.append("ocr")
            if env_bool("SUPPORTS_TOOL_CALLING"):
                capabilities.append("tools")
            if env_bool("SUPPORTS_REASONING"):
                capabilities.append("reasoning")
            managed_models = [(model_name, capabilities)]
        combos = self.omni.combos()
        combos_by_name = {str(item.get("name", "")): item for item in combos}
        stamp = now()
        seeded_names: set[str] = set()
        with self.db.connect() as connection:
            for model_name, capabilities in managed_models:
                combo = combos_by_name.get(model_name)
                if not combo or not str(combo.get("id", "")):
                    continue
                existing = connection.execute(
                    "SELECT id FROM published_models WHERE public_model_id=?",
                    (model_name,),
                ).fetchone()
                if existing:
                    connection.execute(
                        "UPDATE published_models SET source_kind='combo',source_ref=?,"
                        "source_model=?,capabilities_json=?,health_status='healthy',"
                        "health_failures=0,updated_at=? WHERE id=?",
                        (
                            str(combo["id"]),
                            model_name,
                            json.dumps(capabilities),
                            stamp,
                            existing["id"],
                        ),
                    )
                    model_id = existing["id"]
                else:
                    model_id = str(uuid.uuid4())
                    connection.execute(
                        "INSERT INTO published_models(id,public_model_id,display_name,description,source_kind,source_ref,source_model,capabilities_json,status,health_status,last_health_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            model_id, model_name, model_name,
                            AUTO_SEEDED_MODEL_DESCRIPTION, "combo", str(combo["id"]),
                            model_name, json.dumps(capabilities), "published", "healthy",
                            stamp, stamp, stamp,
                        ),
                    )
                    connection.execute(
                        "INSERT INTO model_price_versions(model_id,effective_at,input_price_micros,output_price_micros,cached_price_micros,reasoning_price_micros,actor) VALUES(?,?,?,?,?,?,?)",
                        (model_id, stamp, 0, 0, 0, 0, "system"),
                    )
                connection.execute(
                    "INSERT OR IGNORE INTO model_access(model_id,subject_type,subject_id,created_at) VALUES(?, 'all', '', ?)",
                    (model_id, stamp),
                )
                seeded_names.add(model_name)
            if registry_owned and seeded_names:
                placeholders = ",".join("?" for _ in seeded_names)
                connection.execute(
                    "UPDATE published_models SET health_status='failed',"
                    "health_failures=MAX(health_failures,3),updated_at=? "
                    "WHERE description=? AND public_model_id NOT IN ("
                    + placeholders
                    + ")",
                    (stamp, AUTO_SEEDED_MODEL_DESCRIPTION, *sorted(seeded_names)),
                )

    @staticmethod
    def _source_combo(
        combos: list[dict[str, Any]], source_ref: str, source_model: str
    ) -> dict[str, Any] | None:
        return next(
            (
                combo
                for combo in combos
                if (source_ref and str(combo.get("id", "")) == source_ref)
                or (source_model and str(combo.get("name", "")) == source_model)
            ),
            None,
        )

    @staticmethod
    def _combo_member_signatures(combo: dict[str, Any] | None) -> list[str]:
        """返回兼容不同 Schema 的 Combo 直接成员标识集合。"""
        if not combo:
            return []
        members = combo.get("models", combo.get("targets", []))
        if not isinstance(members, list):
            return []
        signatures: list[str] = []
        for member in members:
            if isinstance(member, str):
                identity: dict[str, Any] = {"member": member}
            elif isinstance(member, dict):
                identity = {
                    "kind": str(member.get("kind", "model")),
                    "provider": str(
                        member.get("provider", member.get("providerId", ""))
                    ),
                    "model": str(
                        member.get(
                            "model",
                            member.get(
                                "modelId",
                                member.get("qualifiedModel", member.get("id", "")),
                            ),
                        )
                    ),
                    "connection": str(
                        member.get(
                            "connectionId",
                            member.get("providerConnectionId", member.get("connection", "")),
                        )
                    ),
                    "combo": str(
                        member.get(
                            "comboName",
                            member.get("comboId", member.get("combo_id", "")),
                        )
                    ),
                }
            else:
                continue
            signatures.append(json.dumps(identity, sort_keys=True, separators=(",", ":")))
        return sorted(signatures)

    @classmethod
    def _public_combo_mirrors_source(
        cls, public_combo: dict[str, Any] | None, source_combo: dict[str, Any] | None
    ) -> bool:
        public_members = cls._combo_member_signatures(public_combo)
        source_members = cls._combo_member_signatures(source_combo)
        if not public_members or public_members != source_members:
            return False
        public_strategy = str((public_combo or {}).get("strategy", "round-robin"))
        source_strategy = str((source_combo or {}).get("strategy", "round-robin"))
        if public_strategy != source_strategy:
            return False
        source_config = (source_combo or {}).get("config", {})
        public_config = (public_combo or {}).get("config", {})
        if isinstance(source_config, dict):
            if not isinstance(public_config, dict):
                return False
            for key, value in source_config.items():
                if public_config.get(key) != value:
                    return False
        return True

    def ensure_public_combo_route(
        self,
        public_id: str,
        source_ref: str,
        source_model: str,
        published: bool,
    ) -> dict[str, Any]:
        """把基于 Combo 的公开模型 ID 发布为真实原生 Combo。

        OmniRoute Responses API 会先解析裸模型名，再应用模型到 Combo 的映射。
        只有映射的公开 ID 可能因此被改写为带 Provider 的模型，并被 Key 策略
        拒绝。公开 Combo 必须镜像来源 Combo 的直接成员和路由配置。OmniRoute
        3.8.x 虽允许嵌套 Combo，却不会保留内层轮询游标，高负载时会把流量集中
        到单个 Worker，因此这里始终展开为直接成员。
        """
        combos = self.omni.combos()
        source = self._source_combo(combos, source_ref, source_model)
        if not source:
            raise ValueError("combo id is required")
        source_id = str(source.get("id", ""))
        source_name = str(source.get("name", ""))
        if not source_id or not source_name:
            raise RuntimeError("OmniRoute returned an invalid source combo")
        if public_id == source_name:
            return {
                "mapping_kind": "source-combo",
                "mapping_id": source_id,
                "source_ref": source_id,
                "source_model": source_name,
                "created": False,
            }

        existing = next(
            (combo for combo in combos if str(combo.get("name", "")) == public_id),
            None,
        )
        if existing and str(existing.get("description", "")) not in LLMCTL_MANAGED_COMBO_DESCRIPTIONS:
            raise RuntimeError(
                f"OmniRoute combo {public_id!r} already exists and is not managed by LLMCtl"
            )
        source_models = source.get("models", source.get("targets", []))
        if not isinstance(source_models, list) or not source_models:
            raise RuntimeError("source combo has no routable members")
        payload: dict[str, Any] = {
            "name": public_id,
            "description": PUBLIC_COMBO_MANAGED_DESCRIPTION,
            # JSON 往返提供符合 schema 的深拷贝，不与来源响应共享可变成员/配置。
            "models": json.loads(json.dumps(source_models)),
            "strategy": str(source.get("strategy", "round-robin")) or "round-robin",
        }
        source_config = source.get("config")
        if isinstance(source_config, dict):
            payload["config"] = json.loads(json.dumps(source_config))
        context_length = source.get("context_length")
        if isinstance(context_length, int) and context_length >= 1000:
            payload["context_length"] = context_length
        combo_id, created = self.omni.upsert_combo(
            str(existing.get("id", "")) if existing else "",
            payload,
            active=published,
        )
        return {
            "mapping_kind": "native-combo",
            "mapping_id": combo_id,
            "source_ref": source_id,
            "source_model": source_name,
            "created": created,
            "updated": bool(existing) and not self._public_combo_mirrors_source(
                existing, source
            ),
        }

    def _delete_published_route(
        self, mapping_kind: str, mapping_id: str, public_id: str
    ) -> None:
        if mapping_kind == "combo" and mapping_id:
            self.omni.delete_combo_mapping(mapping_id)
        elif mapping_kind == "native-combo" and mapping_id:
            self.omni.delete_combo(mapping_id)
        elif mapping_kind == "alias" and public_id:
            self.omni.delete_model_alias(public_id)

    def reconcile_public_combo_routes(self) -> dict[str, int]:
        """在不中断流量的前提下幂等迁移旧 Combo 映射。"""
        self.prepare_public_combo_migration_backup()
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM published_models WHERE source_kind='combo' "
                "AND health_status!='failed' "
                "ORDER BY created_at,id"
            ).fetchall()
        migrated = unchanged = failed = 0
        for row in rows:
            old_kind = str(row["mapping_kind"] or "")
            old_id = str(row["mapping_id"] or "")
            public_id = str(row["public_model_id"] or "")
            try:
                route = self.ensure_public_combo_route(
                    public_id,
                    str(row["source_ref"] or ""),
                    str(row["source_model"] or ""),
                    str(row["status"] or "") == "published",
                )
                new_kind = str(route["mapping_kind"])
                new_id = str(route["mapping_id"])
                changed = (
                    old_kind != new_kind
                    or old_id != new_id
                    or str(row["source_ref"] or "") != str(route["source_ref"])
                    or str(row["source_model"] or "") != str(route["source_model"])
                    or bool(route.get("updated"))
                )
                # 原生路由已上线；只停用被替代的门户路由，用户 Key 继续使用相同的
                # allowlist Combo ID。
                if old_kind in {"combo", "native-combo", "alias"} and (
                    old_kind != new_kind or old_id != new_id
                ):
                    try:
                        self._delete_published_route(old_kind, old_id, public_id)
                    except RuntimeError as error:
                        if "HTTP 404" not in str(error):
                            raise
                with self.db.connect() as connection:
                    connection.execute(
                        "UPDATE published_models SET source_ref=?,source_model=?,"
                        "mapping_kind=?,mapping_id=?,updated_at=? WHERE id=?",
                        (
                            route["source_ref"],
                            route["source_model"],
                            new_kind,
                            new_id,
                            now(),
                            row["id"],
                        ),
                    )
                migrated += 1 if changed else 0
                unchanged += 0 if changed else 1
            except Exception as error:
                failed += 1
                print(
                    f"[account-portal] public combo reconciliation warning "
                    f"(model={public_id}): {error}",
                    file=sys.stderr,
                    flush=True,
                )
        if failed == 0:
            self.db.update_settings(
                {PUBLIC_COMBO_MIGRATION_SETTING: PUBLIC_COMBO_MIGRATION_NAME}
            )
        return {"migrated": migrated, "unchanged": unchanged, "failed": failed}

    def public_combo_route_status(self) -> dict[str, Any]:
        """比较门户已发布的 Combo ID 与 OmniRoute 当前原生 Combo。"""
        combos = self.omni.combos()
        by_name = {str(item.get("name", "")): item for item in combos}
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT public_model_id,source_ref,source_model,mapping_kind,"
                "mapping_id,status FROM published_models WHERE source_kind='combo' "
                "ORDER BY public_model_id"
            ).fetchall()
        routes: list[dict[str, Any]] = []
        for row in rows:
            public_id = str(row["public_model_id"] or "")
            mapping_kind = str(row["mapping_kind"] or "")
            mapping_id = str(row["mapping_id"] or "")
            live = by_name.get(public_id)
            live_id = str(live.get("id", "")) if live else ""
            description = str(live.get("description", "")) if live else ""
            source = self._source_combo(
                combos,
                str(row["source_ref"] or ""),
                str(row["source_model"] or ""),
            )
            identity_route = (
                public_id == str(row["source_model"] or "")
                and live_id == str(row["source_ref"] or "")
            )
            managed_route = description in LLMCTL_MANAGED_COMBO_DESCRIPTIONS
            mirrored_route = self._public_combo_mirrors_source(live, source)
            ready = bool(live) and (
                (
                    mapping_kind == "native-combo"
                    and managed_route
                    and mirrored_route
                    and mapping_id == live_id
                )
                or (mapping_kind == "source-combo" and identity_route and mapping_id == live_id)
            )
            if not live:
                reason = "native combo missing"
            elif mapping_kind not in {"native-combo", "source-combo"}:
                reason = f"legacy mapping kind: {mapping_kind or 'unset'}"
            elif mapping_id != live_id:
                reason = "portal mapping id does not match live combo"
            elif mapping_kind == "native-combo" and not managed_route:
                reason = "live combo is not managed by LLMCtl"
            elif mapping_kind == "native-combo" and not mirrored_route:
                reason = "native combo does not mirror source members"
            elif mapping_kind == "source-combo" and not identity_route:
                reason = "source combo identity does not match"
            else:
                reason = "ready"
            routes.append(
                {
                    "public_model_id": public_id,
                    "published": str(row["status"] or "") == "published",
                    "mapping_kind": mapping_kind or "legacy",
                    "mapping_id": mapping_id,
                    "live_combo_id": live_id,
                    "source_model": str(row["source_model"] or ""),
                    "member_count": len(self._combo_member_signatures(live)),
                    "source_member_count": len(self._combo_member_signatures(source)),
                    "ready": ready,
                    "reason": reason,
                }
            )
        ready_count = sum(1 for route in routes if route["ready"])
        return {
            "ready": ready_count == len(routes),
            "ready_count": ready_count,
            "total": len(routes),
            "routes": routes,
        }

    def effective_models(self, user_id: str) -> list[dict[str, Any]]:
        with self.db.connect() as connection:
            account = connection.execute(
                "SELECT * FROM billing_accounts WHERE user_id=?", (user_id,)
            ).fetchone()
            models = self.rows(
                connection.execute(
                    """SELECT DISTINCT p.* FROM published_models p
                       JOIN model_access a ON a.model_id=p.id
                       WHERE p.status='published' AND p.health_status!='failed' AND (
                         a.subject_type='all' OR
                         (a.subject_type='user' AND a.subject_id=?) OR
                         (a.subject_type='group' AND a.subject_id IN
                           (SELECT m.group_id FROM user_group_members m
                            JOIN user_groups g ON g.id=m.group_id
                            WHERE m.user_id=? AND g.status='active'))
                       ) ORDER BY p.public_model_id""",
                    (user_id, user_id),
                ).fetchall()
            )
        balance = int(account["balance_micros"]) if account else 0
        suspended = bool(account["suspended"]) if account else False
        result = []
        for model in models:
            paid = any(
                int(model[key]) > 0
                for key in (
                    "input_price_micros", "output_price_micros", "cached_price_micros",
                    "reasoning_price_micros",
                )
            )
            if not suspended and (not paid or balance > 0):
                result.append(model)
        return result

    def sync_user(self, user_id: str) -> None:
        # 权限发布、用量结算和管理员策略变更共用一把锁，避免维护 tick 在故障关闭
        # 更新期间用旧策略重新启用 Key。
        with self.lock:
            self._sync_user(user_id)

    def _sync_user(self, user_id: str) -> None:
        with self.db.connect() as connection:
            user = connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user or not user["api_key_id"]:
            return
        # 旧 LLMCtl 会把促销赠额镜像为 OmniRoute 全局 Token 限制，赠额耗尽后即使
        # 有现金余额也无法使用。发布当前计费策略前移除该旧硬限制；删除或后续
        # 策略同步任一步失败，Key 都保持禁用。
        if user["token_limit_id"]:
            self.omni.activate_key(str(user["api_key_id"]), False)
            try:
                self.omni.delete_limit(str(user["token_limit_id"]))
            except RuntimeError as error:
                if "HTTP 404" not in str(error):
                    raise
            with self.db.connect() as connection:
                connection.execute(
                    "UPDATE users SET token_limit_id=NULL WHERE id=?",
                    (user_id,),
                )
            with self.db.connect() as connection:
                user = connection.execute(
                    "SELECT * FROM users WHERE id=?", (user_id,)
                ).fetchone()
        models = self.effective_models(user_id) if user["status"] == "active" else []
        allowed_models: list[str] = []
        allowed_combos: list[str] = []
        for model in models:
            # 只授权公开 ID。OmniRoute 在 API Key 策略检查后解析门户所有的 Combo
            # 映射/模型别名；若加入来源 ID，用户可直接调用底层路由绕过公开命名
            # 与访问策略。
            public_id = str(model["public_model_id"])
            target = allowed_combos if model["source_kind"] == "combo" else allowed_models
            if public_id and public_id not in target:
                target.append(public_id)
        try:
            self.omni.patch_key_permissions(
                user["api_key_id"], allowed_models, allowed_combos,
                user["status"] == "active" and bool(models),
                int(user["max_sessions"]),
                int(user["requests_per_minute"]),
                int(user["requests_per_day"]),
            )
            sync_status, error = "synced", ""
        except Exception as exc:
            sync_status, error = "failed", str(exc)[:500]
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO permission_sync(user_id,status,error,updated_at) VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET status=excluded.status,error=excluded.error,updated_at=excluded.updated_at",
                (user_id, sync_status, error, now()),
            )
        if error:
            raise RuntimeError(error)

    def quiesce_all_users(self) -> int:
        """修改多个 API Key 共享策略前先关闭调用权限，确保失败时保持拒绝。"""
        with self.db.connect() as connection:
            keys = [
                str(row["api_key_id"])
                for row in connection.execute(
                    "SELECT api_key_id FROM users WHERE role='user' AND api_key_id IS NOT NULL"
                )
            ]
        disabled: list[str] = []
        try:
            for key_id in keys:
                self.omni.activate_key(key_id, False)
                disabled.append(key_id)
        except Exception:
            # 静默操作无法完成时恢复最后提交的有效策略，此时请求的策略修改尚未开始。
            with contextlib.suppress(Exception):
                self.sync_all_users()
            raise RuntimeError("could not safely quiesce all user API keys")
        return len(disabled)

    def sync_all_users(self) -> dict[str, int]:
        with self.db.connect() as connection:
            ids = [row["id"] for row in connection.execute("SELECT id FROM users WHERE role='user'")]
        success = failed = 0
        for user_id in ids:
            try:
                self.sync_user(user_id)
                success += 1
            except RuntimeError:
                failed += 1
        return {"synced": success, "failed": failed}

    def discover_free_resources(self) -> dict[str, int]:
        catalog = self.omni.free_models()
        rankings = self.omni.free_rankings()
        available_rankings = self.omni.free_rankings(available_only=True)
        configured = {str(item.get("id", "")): item for item in rankings}
        available = {str(item.get("id", "")): item for item in available_rankings}
        stamp = now()
        seen: set[str] = set()
        with self.db.connect() as connection:
            for item in catalog:
                provider = str(item.get("provider", "")).strip()
                model_id = str(item.get("modelId", "")).strip()
                if not provider or not model_id:
                    continue
                resource_key = f"{provider}:{model_id}"
                seen.add(resource_key)
                provider_state = configured.get(provider)
                source = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                connection.execute(
                    """INSERT INTO free_resources(resource_key,provider,model_id,display_name,free_type,monthly_tokens,credit_tokens,terms_status,configured,available,source_json,discovered_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(resource_key) DO UPDATE SET
                       display_name=excluded.display_name,free_type=excluded.free_type,monthly_tokens=excluded.monthly_tokens,
                       credit_tokens=excluded.credit_tokens,terms_status=excluded.terms_status,configured=excluded.configured,
                       available=excluded.available,source_json=excluded.source_json,updated_at=excluded.updated_at""",
                    (
                        resource_key, provider, model_id, str(item.get("displayName", model_id)),
                        str(item.get("freeType", "unknown")), item.get("monthlyTokens"), item.get("creditTokens"),
                        str(item.get("tos", "")), 1 if provider_state else 0, 1 if provider in available else 0,
                        source, stamp, stamp,
                    ),
                )
            if seen:
                placeholders = ",".join("?" for _ in seen)
                connection.execute(
                    f"UPDATE free_resources SET available=0,updated_at=? WHERE resource_key NOT IN ({placeholders})",
                    (stamp, *sorted(seen)),
                )
        visibility = self.refresh_free_resource_visibility()
        self.free_visibility_reconciled_at = now()
        return {
            "catalog": len(catalog),
            "configured_providers": len(configured),
            "available_providers": len(available),
            "resources": len(seen),
            "hidden_resources": visibility["hidden"],
        }

    def refresh_free_resource_visibility(self) -> dict[str, int]:
        """镜像 OmniRoute 原生隐藏状态，但不取得该状态的所有权。"""
        with self.db.connect() as connection:
            providers = [
                str(row["provider"])
                for row in connection.execute(
                    "SELECT DISTINCT provider FROM free_resources WHERE configured=1"
                )
            ]
        hidden_total = reconciled = failed = 0
        for provider in providers:
            try:
                hidden = self.omni.hidden_provider_models(provider)
            except RuntimeError:
                failed += 1
                continue
            with self.db.connect() as connection:
                rows = connection.execute(
                    "SELECT resource_key,model_id FROM free_resources WHERE provider=?",
                    (provider,),
                ).fetchall()
                for row in rows:
                    visible = 0 if str(row["model_id"]) in hidden else 1
                    hidden_total += 1 - visible
                    connection.execute(
                        "UPDATE free_resources SET native_visible=?,updated_at=? WHERE resource_key=?",
                        (visible, now(), row["resource_key"]),
                    )
            reconciled += 1
        return {"providers": reconciled, "failed": failed, "hidden": hidden_total}

    def test_free_resource(self, resource_key: str) -> dict[str, Any]:
        with self.db.connect() as connection:
            resource = connection.execute(
                "SELECT * FROM free_resources WHERE resource_key=?", (resource_key,)
            ).fetchone()
        if not resource:
            raise ValueError("free resource not found")
        if not resource["configured"]:
            error = f"请先配置并启用该免费资源对应的供应商：{resource['provider']}"
            with self.db.connect() as connection:
                connection.execute(
                    "UPDATE free_resources SET test_status='failed',test_error=?,available=0,last_tested_at=?,updated_at=? WHERE resource_key=?",
                    (error, now(), now(), resource_key),
                )
            raise ValueError(error)
        try:
            # 免费 Provider 可能要求流式、专用端点、能力协商或由 OmniRoute 选择
            # Connection。复用原生仪表盘探针，让门户与原生 UI 共享同一测试契约，
            # 避免两套略有差异的适配器。
            latency, content = self.omni.test_provider_model(
                str(resource["provider"]), str(resource["model_id"])
            )
            status, available, error = "healthy", 1, ""
        except Exception as exc:
            latency, content, status, available, error = None, "", "failed", 0, str(exc)[:500]
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE free_resources SET test_status=?,test_latency_ms=?,test_error=?,available=?,last_tested_at=?,updated_at=? WHERE resource_key=?",
                (status, latency, error, available, now(), now(), resource_key),
            )
        if error:
            raise RuntimeError(error)
        return {"status": status, "latency_ms": latency, "response": content}

    def save_model(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        """校验、测试并保存公开模型，同时同步管理员明确修改的原生上限。

        参数：
            payload: 公开 ID、来源、能力、价格、访问范围和可选原生参数。
            actor: 执行本次管理操作的审计主体。

        返回：
            保存后的模型、测试状态和接入层参数同步结果。

        异常：
            ValueError: 字段、来源、权限或输出上限违反公开模型契约。
            RuntimeError: 接入层测试、映射或参数同步失败。
        """

        public_id = str(payload.get("public_model_id", "")).strip()
        source_kind = str(payload.get("source_kind", "")).strip()
        source_model = str(payload.get("source_model", "")).strip()
        source_ref = str(payload.get("source_ref", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,200}", public_id):
            raise ValueError("invalid public model id")
        if source_kind not in {"combo", "model", "free"} or not source_model:
            raise ValueError("invalid source model")
        capabilities = payload.get("capabilities", ["chat"])
        if not isinstance(capabilities, list) or any(
            not isinstance(item, str) or not re.fullmatch(r"[a-z0-9_-]{1,32}", item)
            for item in capabilities
        ):
            raise ValueError("invalid capabilities")
        status = str(payload.get("status", "published"))
        if status not in {"draft", "published", "disabled"}:
            raise ValueError("invalid model status")
        access = payload.get("access") or [{"type": "all", "id": ""}]
        if not isinstance(access, list):
            raise ValueError("invalid model access")
        prices = {
            key: money_to_micros(payload.get(key.replace("_micros", ""), "0"))
            for key in (
                "input_price_micros", "output_price_micros", "cached_price_micros",
                "reasoning_price_micros",
            )
        }
        if any(value < 0 for value in prices.values()):
            raise ValueError("model prices cannot be negative")
        requested_context = positive_int_or_none(
            payload.get("context_window_tokens"), "最大上下文"
        )
        requested_output = positive_int_or_none(
            payload.get("max_output_tokens"),
            "最大输出 Token",
            max_output_tokens_limit(),
        )
        sync_context = bool(payload.get("sync_context_window"))
        sync_output = bool(payload.get("sync_max_output_tokens"))
        if source_kind == "free":
            with self.db.connect() as connection:
                free = connection.execute(
                    "SELECT * FROM free_resources WHERE resource_key=?", (source_ref,)
                ).fetchone()
            if not free or free["test_status"] != "healthy" or not free["available"]:
                raise ValueError("free resource must pass a live test before publishing")
        with self.db.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM published_models WHERE id=? OR public_model_id=? LIMIT 1",
                (str(payload.get("id", "")), public_id),
            ).fetchone()
        self.quiesce_all_users()
        old_mapping_id = str(existing["mapping_id"] or "") if existing else ""
        old_mapping_kind = str(existing["mapping_kind"] or "") if existing else ""
        old_public_id = str(existing["public_model_id"] or "") if existing else ""
        old_source_ref = str(existing["source_ref"] or "") if existing else ""
        old_source_model = str(existing["source_model"] or "") if existing else ""
        old_status = str(existing["status"] or "") if existing else ""
        mapping_id = ""
        mapping_kind = ""
        created_mapping = False
        mutated_mapping = False
        try:
            if source_kind == "combo":
                route = self.ensure_public_combo_route(
                    public_id,
                    source_ref,
                    source_model,
                    status == "published",
                )
                mapping_id = str(route["mapping_id"])
                mapping_kind = str(route["mapping_kind"])
                source_ref = str(route["source_ref"])
                source_model = str(route["source_model"])
                created_mapping = bool(route["created"])
                mutated_mapping = bool(
                    existing
                    and not created_mapping
                    and old_mapping_kind == "native-combo"
                    and old_mapping_id == mapping_id
                    and old_public_id == public_id
                )
            elif public_id != source_model:
                mapping_id = self.omni.set_model_alias(public_id, source_model)
                mapping_kind = "alias"
                mutated_mapping = old_mapping_kind == "alias" and old_public_id == public_id
                created_mapping = not mutated_mapping
            if status == "published":
                latency, _ = self.omni.test_model(public_id)
            else:
                latency = None
            inspect_payload = dict(payload)
            inspect_payload.update(
                {
                    "source_kind": source_kind,
                    "source_ref": source_ref,
                    "source_model": source_model,
                }
            )
            metadata: dict[str, Any]
            metadata_error = ""
            try:
                metadata = self.inspect_model(inspect_payload)
            except Exception as error:
                metadata = {"targets": [], "read_at": now()}
                metadata_error = str(error)[:2000]
            context_window = requested_context or metadata.get("context_window_tokens")
            max_output = requested_output or metadata.get("max_output_tokens")
            if (sync_context or sync_output) and metadata_error:
                metadata_status = "failed"
            else:
                metadata_status, sync_error = self._sync_model_limits(
                    metadata,
                    context_window,
                    max_output,
                    sync_context,
                    sync_output,
                )
                metadata_error = sync_error or metadata_error
            stamp = now()
            model_id = str(existing["id"]) if existing else str(uuid.uuid4())
            with self.db.connect() as connection:
                connection.execute(
                    """INSERT INTO published_models(id,public_model_id,display_name,description,source_kind,source_ref,source_provider,source_model,capabilities_json,input_price_micros,output_price_micros,cached_price_micros,reasoning_price_micros,status,upstream_free,mapping_kind,mapping_id,health_status,health_latency_ms,last_health_at,health_failures,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                       public_model_id=excluded.public_model_id,display_name=excluded.display_name,description=excluded.description,
                       source_kind=excluded.source_kind,source_ref=excluded.source_ref,source_provider=excluded.source_provider,
                       source_model=excluded.source_model,capabilities_json=excluded.capabilities_json,input_price_micros=excluded.input_price_micros,
                       output_price_micros=excluded.output_price_micros,cached_price_micros=excluded.cached_price_micros,
                       reasoning_price_micros=excluded.reasoning_price_micros,status=excluded.status,upstream_free=excluded.upstream_free,
                       mapping_kind=excluded.mapping_kind,mapping_id=excluded.mapping_id,health_status=excluded.health_status,
                       health_latency_ms=excluded.health_latency_ms,last_health_at=excluded.last_health_at,health_failures=0,updated_at=excluded.updated_at""",
                    (
                        model_id, public_id, str(payload.get("display_name", public_id)), str(payload.get("description", "")),
                        source_kind, source_ref, str(payload.get("source_provider", "")), source_model,
                        json.dumps(capabilities, ensure_ascii=False), prices["input_price_micros"], prices["output_price_micros"],
                        prices["cached_price_micros"], prices["reasoning_price_micros"], status,
                        1 if source_kind == "free" else 0, mapping_kind, mapping_id,
                        "healthy" if status == "published" else "unknown", latency, stamp if latency else None,
                        0, int(existing["created_at"]) if existing else stamp, stamp,
                    ),
                )
                connection.execute(
                    """UPDATE published_models
                       SET context_window_tokens=?,max_output_tokens=?,metadata_json=?,
                           metadata_sync_status=?,metadata_sync_error=?,metadata_synced_at=?,updated_at=?
                       WHERE id=?""",
                    (
                        context_window,
                        max_output,
                        json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                        metadata_status,
                        metadata_error,
                        stamp,
                        stamp,
                        model_id,
                    ),
                )
                connection.execute("DELETE FROM model_access WHERE model_id=?", (model_id,))
                for item in access:
                    subject_type = str(item.get("type", "")) if isinstance(item, dict) else ""
                    subject_id = str(item.get("id", "")) if isinstance(item, dict) else ""
                    if subject_type not in {"all", "group", "user"}:
                        raise ValueError("invalid access subject")
                    if subject_type == "group" and not connection.execute(
                        "SELECT 1 FROM user_groups WHERE id=?", (subject_id,)
                    ).fetchone():
                        raise ValueError("access group does not exist")
                    if subject_type == "user" and not connection.execute(
                        "SELECT 1 FROM users WHERE id=? AND role='user'", (subject_id,)
                    ).fetchone():
                        raise ValueError("access user does not exist")
                    connection.execute(
                        "INSERT INTO model_access(model_id,subject_type,subject_id,created_at) VALUES(?,?,?,?)",
                        (model_id, subject_type, subject_id if subject_type != "all" else "", stamp),
                    )
                previous = connection.execute(
                    "SELECT input_price_micros,output_price_micros,cached_price_micros,reasoning_price_micros FROM model_price_versions WHERE model_id=? ORDER BY effective_at DESC LIMIT 1",
                    (model_id,),
                ).fetchone()
                current_prices = tuple(prices[key] for key in prices)
                if not previous or tuple(previous) != current_prices:
                    connection.execute(
                        "INSERT INTO model_price_versions(model_id,effective_at,input_price_micros,output_price_micros,cached_price_micros,reasoning_price_micros,actor) VALUES(?,?,?,?,?,?,?)",
                        (model_id, stamp, *current_prices, actor),
                    )
            permission_sync = self.sync_all_users()
            # 新 OmniRoute 路由、门户策略和用户权限全部提交后才删除旧别名/映射。
            if old_mapping_kind in {"combo", "native-combo", "alias"} and (
                old_mapping_kind != mapping_kind
                or old_mapping_id != mapping_id
                or old_public_id != public_id
            ):
                with contextlib.suppress(Exception):
                    self._delete_published_route(
                        old_mapping_kind, old_mapping_id, old_public_id
                    )
            return {
                "id": model_id,
                "public_model_id": public_id,
                "latency_ms": latency,
                "permission_sync": permission_sync,
                "metadata_sync": {
                    "status": metadata_status,
                    "error": metadata_error,
                    "targets": len(metadata.get("targets", [])),
                },
            }
        except Exception:
            if created_mapping:
                with contextlib.suppress(Exception):
                    self._delete_published_route(
                        mapping_kind, mapping_id, public_id
                    )
            elif mutated_mapping and existing:
                # 在线测试、策略校验或 SQLite 持久化失败时恢复最后提交的 OmniRoute 映射。
                with contextlib.suppress(Exception):
                    if old_mapping_kind == "native-combo":
                        self.ensure_public_combo_route(
                            old_public_id,
                            old_source_ref,
                            old_source_model,
                            old_status == "published",
                        )
                    elif old_mapping_kind == "alias":
                        self.omni.set_model_alias(old_public_id, old_source_model)
            with contextlib.suppress(Exception):
                self.sync_all_users()
            raise

    def test_published_model(self, model_id: str) -> dict[str, Any]:
        with self.db.connect() as connection:
            model = connection.execute("SELECT * FROM published_models WHERE id=?", (model_id,)).fetchone()
        if not model:
            raise ValueError("model not found")
        try:
            latency, response = self.omni.test_model(model["public_model_id"])
            failures, health, error = 0, "healthy", ""
        except Exception as exc:
            latency, response, failures, error = None, "", int(model["health_failures"] or 0) + 1, str(exc)[:500]
            health = "failed" if failures >= 3 else "unknown"
        withdraw = bool(model["upstream_free"]) and health == "failed"
        if withdraw:
            # 免费上游可能随时消失；撤回共享路由前先停用所有门户 Key，避免请求在
            # 健康状态变化与权限刷新之间穿透。
            self.quiesce_all_users()
            try:
                if model["mapping_kind"] == "combo" and model["mapping_id"]:
                    self.omni.set_combo_mapping(
                        model["public_model_id"], model["source_ref"], model["mapping_id"], False
                    )
                elif model["mapping_kind"] == "native-combo" and model["mapping_id"]:
                    self.omni.set_combo_active(str(model["mapping_id"]), False)
                elif model["mapping_kind"] == "alias":
                    self.omni.delete_model_alias(model["public_model_id"])
            except Exception:
                # 无法确认原生撤回成功时 Key 按设计保持禁用，由管理员修复并重同步。
                with self.db.connect() as connection:
                    connection.execute(
                        "UPDATE published_models SET status='error',health_status='failed',health_error=?,last_health_at=?,health_failures=?,updated_at=? WHERE id=?",
                        (error or "native route withdrawal failed", now(), failures, now(), model_id),
                    )
                raise
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE published_models SET health_status=?,health_latency_ms=?,health_error=?,last_health_at=?,health_failures=?,status=CASE WHEN ?='failed' AND upstream_free=1 THEN 'error' ELSE status END,updated_at=? WHERE id=?",
                (health, latency, error, now(), failures, health, now(), model_id),
            )
        if withdraw:
            self.sync_all_users()
        if error:
            raise RuntimeError(error)
        return {"status": health, "latency_ms": latency, "response": response}
