from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RECOGNITION_")

    # Shared key: the self-hosted / legacy way in. Optional once `trusted_vaults` is set.
    api_key: str | None = None
    # Comma-separated vault hostnames whose drop tokens authenticate a dictation (no key to type). The
    # recognition service asks that vault whether the token is alive; it never holds a key that reads.
    trusted_vaults: str = ""
    # Credential of the remote worker that pulls jobs; the phone key must never work on its routes.
    worker_key: str | None = None
    lease_s: float = 120
    max_attempts: int = 3
    # Audio waits in memory until a worker takes it: bound what can pile up.
    max_queued: int = 50
    max_queued_bytes: int = 200 * 1024 * 1024
    ticket_ttl_s: int = 172800
    tombstone_ttl_s: int = 604800
    sync_timeout_s: float = 30
    max_wait_s: float = 25
    max_audio_bytes: int = 10 * 1024 * 1024
    sweep_interval_s: float = 30
    # Checking a drop token with the user's vault (`GET /v1/me`).
    vault_timeout_s: float = 10
    # Tests only: allow http and non-public hosts.
    vault_allow_insecure: bool = False

    @property
    def trusted_vault_hosts(self) -> frozenset[str]:
        return frozenset(h.strip().lower() for h in self.trusted_vaults.split(",") if h.strip())

    @model_validator(mode="after")
    def _check_worker_key(self) -> "Settings":
        if not self.api_key and not self.trusted_vault_hosts:
            raise ValueError("set api_key or trusted_vaults: some way to authenticate is required")
        if not self.worker_key or len(self.worker_key) < 16:
            raise ValueError("worker_key must be set and at least 16 characters")
        if self.worker_key == self.api_key:
            raise ValueError("worker_key must differ from api_key")
        return self
