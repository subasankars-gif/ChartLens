"""In-memory stand-ins for Firebase Auth and Firestore (unit tests only)."""

from __future__ import annotations

from chartlens_api.appstate import Role, User, UserNotFound, Watchlist
from chartlens_api.auth import AuthenticationError, Claims


class FakeVerifier:
    """Tokens are "uid:email" (verified email) or "unverified:uid:email"."""

    def verify(self, token: str) -> Claims:
        parts = token.split(":")
        if parts[0] == "unverified" and len(parts) == 3:
            return Claims(parts[1], parts[2], email_verified=False)
        if len(parts) != 2:
            raise AuthenticationError("bad token")
        return Claims(parts[0], parts[1], email_verified=True)


class MemoryAppState:
    def __init__(self) -> None:
        self.users: dict[str, User] = {}
        self.watchlists: dict[str, dict[str, Watchlist]] = {}

    def get_user(self, uid: str) -> User | None:
        return self.users.get(uid)

    def create_user(self, user: User) -> User:
        return self.users.setdefault(user.uid, user)

    def update_user(self, uid: str, *, enabled: bool | None, role: Role | None) -> User:
        if uid not in self.users:
            raise UserNotFound(uid)
        u = self.users[uid]
        self.users[uid] = u.model_copy(
            update={k: v for k, v in (("enabled", enabled), ("role", role)) if v is not None}
        )
        return self.users[uid]

    def list_users(self) -> list[User]:
        return sorted(self.users.values(), key=lambda u: u.uid)

    def list_watchlists(self, uid: str) -> list[Watchlist]:
        return sorted(self.watchlists.get(uid, {}).values(), key=lambda w: w.watchlist_id)

    def put_watchlist(self, uid: str, watchlist: Watchlist) -> Watchlist:
        self.watchlists.setdefault(uid, {})[watchlist.watchlist_id] = watchlist
        return watchlist

    def delete_watchlist(self, uid: str, watchlist_id: str) -> bool:
        return self.watchlists.get(uid, {}).pop(watchlist_id, None) is not None
