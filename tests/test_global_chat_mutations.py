import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.v1.endpoints import global_chat as endpoints
from app.services import global_chat_manager as module

USER = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
MESSAGE = "33333333-3333-4333-8333-333333333333"
NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


def row(**values):
    return {"id": MESSAGE, "user_id": USER, "text": "hello", "is_admin": False,
            "is_deleted": False, "deleted_by_user": False, "created_at": NOW - timedelta(minutes=2),
            "server_now": NOW, "edited_at": None, "updated_at": NOW, "revision": 0, **values}


def result(value=None, rows=None):
    return SimpleNamespace(mappings=lambda: SimpleNamespace(first=lambda: value, all=lambda: rows or [], one=lambda: value))


def database(*results):
    session = SimpleNamespace(execute=AsyncMock(side_effect=results), commit=AsyncMock())
    context = MagicMock()
    context.__aenter__.return_value = session
    return session, lambda: context


class MessageMutationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = module.GlobalChatManager()
        self.manager.broadcast_event = AsyncMock()
        self.manager.is_chat_enabled = AsyncMock(return_value=True)
        self.manager.check_rate_limit = AsyncMock(return_value=True)
        self.ban = patch.object(module, "check_user_banned", AsyncMock(return_value={"banned": False}))
        self.ban.start()
        self.addCleanup(self.ban.stop)

    async def test_other_users_cannot_edit_or_delete_even_with_a_known_id(self):
        for deleting in (False, True):
            db, factory = database(result(row()))
            with patch.object(module, "AsyncSessionFactory", factory):
                value = await (self.manager.delete_own_message(MESSAGE, OTHER) if deleting else self.manager.edit_own_message(MESSAGE, OTHER, "changed"))
            self.assertEqual(value["error"], "not_message_owner")
            db.commit.assert_not_awaited()
            self.assertEqual(db.execute.await_count, 1)
        self.manager.broadcast_event.assert_not_awaited()

    async def test_admin_messages_and_missing_messages_are_not_owned(self):
        for original, error in [(row(is_admin=True), "not_message_owner"), (row(user_id=None), "not_message_owner"), (None, "message_not_found")]:
            db, factory = database(result(original))
            with patch.object(module, "AsyncSessionFactory", factory):
                response = await self.manager.delete_own_message(MESSAGE, USER)
            self.assertEqual(response["error"], error)
            db.commit.assert_not_awaited()

    async def test_edit_window_uses_original_creation_and_server_time(self):
        for created in (NOW - timedelta(minutes=15), NOW - timedelta(days=1), NOW + timedelta(seconds=1)):
            db, factory = database(result(row(created_at=created, edited_at=NOW)))
            with patch.object(module, "AsyncSessionFactory", factory):
                response = await self.manager.edit_own_message(MESSAGE, USER, "changed")
            self.assertEqual(response["error"], "edit_window_expired")
            db.commit.assert_not_awaited()

    async def test_edit_before_boundary_locks_and_checks_owner_again_in_update(self):
        original = row(created_at=NOW - timedelta(minutes=15) + timedelta(microseconds=1))
        changed = row(text="changed", edited_at=NOW, revision=1)
        quote = {"message_id": OTHER, "text": "concurrently edited reply", "is_deleted": False,
                 "edited_at": NOW, "reply_to_text": "changed", "revision": 4, "updated_at": NOW}
        db, factory = database(result(original), result(changed), result(rows=[quote]))
        with patch.object(module, "AsyncSessionFactory", factory):
            response = await self.manager.edit_own_message(MESSAGE, USER, "changed")
        self.assertTrue(response["ok"])
        self.assertEqual(response["revision"], 1)
        self.assertEqual(response["reply_updates"][0]["revision"], 4)
        self.assertEqual(response["reply_updates"][0]["text"], "concurrently edited reply")
        self.assertEqual(response["reply_updates"][0]["edited_at"], NOW.isoformat())
        self.assertIn("text, is_deleted, edited_at", str(db.execute.await_args_list[2].args[0]))
        sql = str(db.execute.await_args_list[1].args[0])
        self.assertIn("user_id = CAST(:user_id AS uuid)", sql)
        self.assertIn("interval '15 minutes'", sql)
        self.assertIn("clock_timestamp()", sql)
        self.assertIn("FOR UPDATE", str(db.execute.await_args_list[0].args[0]))
        db.commit.assert_awaited_once()
        self.manager.broadcast_event.assert_awaited_once()

    async def test_window_is_rechecked_when_waiting_for_a_lock(self):
        db, factory = database(result(row()), result(None))
        with patch.object(module, "AsyncSessionFactory", factory):
            response = await self.manager.edit_own_message(MESSAGE, USER, "changed")
        self.assertEqual(response["error"], "edit_window_expired")
        db.commit.assert_not_awaited()
        self.manager.broadcast_event.assert_not_awaited()

    async def test_deletion_has_no_time_limit_erases_text_quotes_and_reactions(self):
        changed = row(is_deleted=True, deleted_by_user=True, text="", revision=1)
        db, factory = database(result(row(created_at=NOW - timedelta(days=10))), result(changed), result(), result())
        with patch.object(module, "AsyncSessionFactory", factory):
            response = await self.manager.delete_own_message(MESSAGE, USER)
        self.assertTrue(response["ok"])
        self.assertTrue(response["is_deleted"])
        self.assertEqual(response["text"], "")
        sql = str(db.execute.await_args_list[1].args[0])
        self.assertIn("text = ''", sql)
        self.assertIn("deleted_by_user = true", sql)
        self.assertIn("user_id = CAST(:user_id AS uuid)", sql)
        self.assertNotIn("15 minutes", sql)
        self.assertEqual(db.execute.await_args_list[2].args[1]["text"], "تم حذف هذه الرسالة")
        self.assertIn("DELETE FROM", str(db.execute.await_args_list[3].args[0]))

    async def test_repeated_deletion_is_idempotent_without_a_second_revision(self):
        original = row(is_deleted=True, deleted_by_user=True, text="", revision=2)
        db, factory = database(result(original), result())
        with patch.object(module, "AsyncSessionFactory", factory):
            response = await self.manager.delete_own_message(MESSAGE, USER)
        self.assertTrue(response["ok"])
        self.assertEqual(response["revision"], 2)
        db.commit.assert_not_awaited()
        self.manager.broadcast_event.assert_not_awaited()

    async def test_deleted_messages_cannot_be_edited_or_restored(self):
        for by_user in (False, True):
            db, factory = database(result(row(is_deleted=True, deleted_by_user=by_user)))
            with patch.object(module, "AsyncSessionFactory", factory):
                response = await self.manager.edit_own_message(MESSAGE, USER, "restored")
            self.assertEqual(response["error"], "message_deleted")
            db.commit.assert_not_awaited()

    async def test_unchanged_text_does_not_mark_as_edited(self):
        db, factory = database(result(row()), result())
        with patch.object(module, "AsyncSessionFactory", factory):
            response = await self.manager.edit_own_message(MESSAGE, USER, "hello")
        self.assertTrue(response["ok"])
        self.assertIsNone(response["edited_at"])
        db.commit.assert_not_awaited()

    async def test_moderation_and_validation_are_not_bypassed_by_editing(self):
        with patch.object(module, "AsyncSessionFactory") as factory:
            for value, error in [("", "empty_message"), (" " * 2, "empty_message"), ("a" * 151, "too_long"), ("fuck", "moderation_blocked")]:
                response = await self.manager.edit_own_message(MESSAGE, USER, value)
                self.assertEqual(response["error"], error)
            factory.assert_not_called()

    async def test_disabled_chat_ban_and_rate_limit_block_edits(self):
        self.manager.is_chat_enabled.return_value = False
        self.assertEqual((await self.manager.edit_own_message(MESSAGE, USER, "changed"))["error"], "chat_disabled")
        self.manager.is_chat_enabled.return_value = True
        with patch.object(module, "check_user_banned", AsyncMock(return_value={"banned": True})):
            self.assertEqual((await self.manager.edit_own_message(MESSAGE, USER, "changed"))["error"], "banned")
        self.manager.check_rate_limit.return_value = False
        self.assertEqual((await self.manager.edit_own_message(MESSAGE, USER, "changed"))["error"], "rate_limited")

    async def test_commit_failure_never_broadcasts_an_uncommitted_change(self):
        db, factory = database(result(row()), result(row(text="changed", revision=1)), result())
        db.commit.side_effect = RuntimeError("database unavailable")
        with patch.object(module, "AsyncSessionFactory", factory), self.assertLogs(module.logger, level="ERROR"):
            response = await self.manager.edit_own_message(MESSAGE, USER, "changed")
        self.assertEqual(response["error"], "internal_error")
        self.manager.broadcast_event.assert_not_awaited()

    async def test_invalid_uuid_is_never_sent_to_database(self):
        with patch.object(module, "AsyncSessionFactory") as factory:
            response = await self.manager.delete_own_message("malicious input", USER)
            self.assertEqual(response["error"], "invalid_id")
            factory.assert_not_called()

    async def test_sync_revision_tracks_mutations_without_relying_on_count_or_clock(self):
        db, factory = database(result({"count": 10, "revision": "22"}))
        with patch.object(module, "AsyncSessionFactory", factory):
            summary = await self.manager.get_message_summary()
        self.assertEqual(summary, {"count": 10, "revision": "22"})
        self.assertIn("SUM(revision)", str(db.execute.await_args.args[0]))
        self.assertIn("deleted_by_user = true", str(db.execute.await_args.args[0]))

    async def test_reply_snapshots_are_loaded_from_the_locked_source_not_client_text(self):
        db, factory = database(result({"id": MESSAGE, "user_name": "original author", "text": "verified text"}), result(row()))
        with patch.object(module, "AsyncSessionFactory", factory):
            await self.manager._insert_message(USER, "name", "", "hello", "normal", False,
                                               {"message_id": MESSAGE, "text": "forged text", "user_name": "forged author"})
        self.assertIn("FOR SHARE", str(db.execute.await_args_list[0].args[0]))
        params = db.execute.await_args_list[1].args[1]
        self.assertEqual(params["reply_to_text"], "verified text")
        self.assertEqual(params["reply_to_user_name"], "original author")

    async def test_reply_to_deleted_source_cannot_reintroduce_a_private_quote(self):
        db, factory = database(result(None), result(row()))
        with patch.object(module, "AsyncSessionFactory", factory):
            await self.manager._insert_message(USER, "name", "", "hello", "normal", False,
                                               {"message_id": MESSAGE, "text": "deleted text"})
        params = db.execute.await_args_list[1].args[1]
        self.assertIsNone(params["reply_to_message_id"])
        self.assertIsNone(params["reply_to_text"])

    async def test_rate_limit_uses_atomic_redis_nx(self):
        redis = SimpleNamespace(set=AsyncMock(side_effect=[True, None]))
        with patch.object(module, "get_redis", AsyncMock(return_value=redis)):
            manager = module.GlobalChatManager()
            self.assertTrue(await manager.check_rate_limit(USER))
            self.assertFalse(await manager.check_rate_limit(USER))
        self.assertEqual(redis.set.await_args.kwargs, {"ex": 5, "nx": True})

    def test_tombstone_serialization_never_leaks_deleted_text_or_quote(self):
        payload = self.manager._serialize_message(row(is_deleted=True, reply_to_text="secret quote", love_count=4))
        self.assertEqual(payload["text"], "")
        self.assertIsNone(payload["reply_to_text"])
        self.assertEqual(payload["love_count"], 0)


class MutationEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_endpoints_use_the_verified_identity_only(self):
        with patch.object(endpoints, "_require_user", AsyncMock(return_value={"id": USER})), \
                patch.object(endpoints.global_chat_manager, "edit_own_message", AsyncMock(return_value={"ok": True})) as edit, \
                patch.object(endpoints.global_chat_manager, "delete_own_message", AsyncMock(return_value={"ok": True})) as delete:
            await endpoints.edit_global_message(uuid.UUID(MESSAGE), endpoints.GlobalChatEditRequest(text="changed"), "Bearer signed-token")
            await endpoints.delete_global_message(uuid.UUID(MESSAGE), "Bearer signed-token")
            self.assertEqual(edit.await_args.kwargs["user_id"], USER)
            self.assertEqual(delete.await_args.kwargs["user_id"], USER)

    def test_client_cannot_supply_owner_timestamps_or_versions(self):
        for field in ["user_id", "created_at", "edited_at", "revision", "is_deleted"]:
            with self.subTest(field=field), self.assertRaises(ValidationError):
                endpoints.GlobalChatEditRequest(text="hello", **{field: "forged"})

    def test_owner_expiry_and_moderation_errors_have_explicit_http_statuses(self):
        for error, status in [("not_message_owner", 403), ("edit_window_expired", 409), ("moderation_blocked", 403), ("rate_limited", 429)]:
            with self.subTest(error=error), self.assertRaises(HTTPException) as caught:
                endpoints._mutation_response({"ok": False, "error": error})
            self.assertEqual(caught.exception.status_code, status)


class MutationHttpTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(endpoints.router)
        self.client = TestClient(app)

    def test_invalid_authentication_never_reaches_mutation(self):
        with patch.object(endpoints, "verify_supabase_token", AsyncMock(return_value=None)), \
                patch.object(endpoints.global_chat_manager, "delete_own_message", AsyncMock()) as mutate:
            response = self.client.post(f"/global-chat/messages/{MESSAGE}/delete", headers={"Authorization": "Bearer invalid"})
        self.assertEqual(response.status_code, 401)
        mutate.assert_not_awaited()

    def test_authenticated_request_for_another_owner_is_forbidden(self):
        db, factory = database(result(row()))
        with patch.object(endpoints, "verify_supabase_token", AsyncMock(return_value={"id": OTHER})), \
                patch.object(module, "AsyncSessionFactory", factory):
            response = self.client.post(f"/global-chat/messages/{MESSAGE}/delete", headers={"Authorization": "Bearer signed"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"]["error"], "not_message_owner")
        db.commit.assert_not_awaited()

    def test_forged_owner_or_timestamp_in_http_body_is_rejected(self):
        with patch.object(endpoints.global_chat_manager, "edit_own_message", AsyncMock()) as mutate:
            response = self.client.post(f"/global-chat/messages/{MESSAGE}/edit", headers={"Authorization": "Bearer signed"},
                                        json={"text": "hello", "user_id": USER, "created_at": NOW.isoformat()})
        self.assertEqual(response.status_code, 422)
        mutate.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
