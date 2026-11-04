"""Distributed download locks (filesystem and S3 arbitration)."""

from __future__ import annotations

import tempfile
import threading
import unittest

from tfproxy.config import S3Config
from tfproxy.locking import LockHandle, LockManager, NullLockManager, lock_key_for
from tfproxy.storage.fs import FilesystemStorage
from tfproxy.storage.multi import TieredStorage
from tfproxy.storage.s3 import S3Storage
from tests.support.fake_s3 import FakeS3

KEY = "modules/hashicorp/consul/aws/5.1.2.zip"


class FakeClock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FilesystemLockTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = FilesystemStorage(self._tmp.name)
        self.clock = FakeClock()
        self.locks = LockManager(self.storage, ttl=60, clock=self.clock, instance="test-a")

    def tearDown(self):
        self._tmp.cleanup()

    def test_lock_key(self):
        self.assertEqual(lock_key_for(KEY), f"locks/{KEY}.lock")

    def test_acquire_release(self):
        handle = self.locks.acquire(KEY)
        self.assertIsInstance(handle, LockHandle)
        self.assertEqual(self.locks.acquire(KEY), None)
        self.assertTrue(self.locks.release(handle))
        self.assertIsNotNone(self.locks.acquire(KEY))

    def test_second_manager_cannot_take_a_held_lock(self):
        other = LockManager(self.storage, ttl=60, clock=self.clock, instance="test-b")
        handle = self.locks.acquire(KEY)
        self.assertIsNotNone(handle)
        self.assertIsNone(other.acquire(KEY))
        # a release only ever deletes a lock whose token matches
        self.assertFalse(other.release(LockHandle(key=KEY, token="not-mine", started_at=0, expires_at=0)))
        self.assertIsNotNone(other.peek(KEY))
        self.assertTrue(self.locks.release(handle))

    def test_stale_lock_is_broken_after_ttl(self):
        first = LockManager(self.storage, ttl=60, clock=self.clock, instance="dead-instance")
        second = LockManager(self.storage, ttl=60, clock=self.clock, instance="survivor")
        self.assertIsNotNone(first.acquire(KEY))
        self.clock.advance(59)
        self.assertIsNone(second.acquire(KEY), "lock is still fresh")
        self.clock.advance(2)
        handle = second.acquire(KEY)
        self.assertIsNotNone(handle, "expired lock must be breakable")
        self.assertEqual(second.broken, 1)
        self.assertIsNone(first.acquire(KEY))

    def test_release_of_expired_lock_does_not_delete_the_new_one(self):
        handle = self.locks.acquire(KEY)
        self.clock.advance(120)
        other = LockManager(self.storage, ttl=60, clock=self.clock, instance="other")
        new_handle = other.acquire(KEY)
        self.assertIsNotNone(new_handle)
        self.assertFalse(self.locks.release(handle))
        self.assertEqual(other.peek(KEY).token, new_handle.token)

    def test_refresh_extends_the_expiry(self):
        handle = self.locks.acquire(KEY)
        self.clock.advance(50)
        self.assertTrue(self.locks.refresh(handle))
        self.clock.advance(5)
        other = LockManager(self.storage, ttl=60, clock=self.clock, instance="other")
        self.assertIsNone(other.acquire(KEY))
        self.clock.advance(60)
        self.assertIsNotNone(other.acquire(KEY))

    def test_refresh_fails_when_the_lock_is_gone(self):
        handle = self.locks.acquire(KEY)
        self.locks.release(handle)
        self.assertFalse(self.locks.refresh(handle))

    def test_refresh_reports_a_lock_someone_else_took_over(self):
        handle = self.locks.acquire(KEY)
        # Another instance broke our (expired) lock and wrote its own record.
        squatter = LockHandle(key=KEY, token="someone-else", started_at=self.clock(), expires_at=self.clock() + 60)
        self.locks._write(squatter)
        self.assertFalse(self.locks.refresh(handle), "the refresh must notice it lost the lock")
        self.assertEqual(self.locks.peek(handle.key).token, "someone-else", "and must not clobber the new owner")

    def test_failed_refresh_leaves_the_handle_untouched(self):
        handle = self.locks.acquire(KEY)
        before = handle.expires_at
        self.locks.release(handle)
        self.assertFalse(self.locks.refresh(handle, ttl=600))
        self.assertEqual(handle.expires_at, before, "a refresh that failed must not move the expiry")

    def test_wait_status_reports_how_the_wait_ended(self):
        self.locks.acquire(KEY)
        self.assertEqual(self.locks.wait_status(KEY, timeout=0.1, poll=0.05), "timeout")
        self.assertEqual(self.locks.wait_status("modules/absent.zip", timeout=0.1), "free")

    def test_wait_for_release_returns_when_unlocked(self):
        handle = self.locks.acquire(KEY)
        released = threading.Event()

        def unlock():
            if released.wait(0.2):
                self.locks.release(handle)

        thread = threading.Thread(target=unlock)
        thread.start()
        released.set()
        self.assertTrue(self.locks.wait_for_release(KEY, timeout=5, poll=0.01))
        thread.join()

    def test_wait_for_release_gives_up_on_timeout(self):
        self.locks.acquire(KEY)
        self.assertFalse(self.locks.wait_for_release(KEY, timeout=0.2, poll=0.05))

    def test_wait_for_release_returns_for_a_dead_holder(self):
        self.locks.acquire(KEY)
        self.clock.advance(1000)
        self.assertTrue(self.locks.wait_for_release(KEY, timeout=0.1, poll=0.05))

    def test_hold_context_manager_releases(self):
        with self.locks.hold(KEY) as handle:
            self.assertIsNotNone(handle)
            self.assertIsNone(self.locks.acquire(KEY))
        self.assertIsNotNone(self.locks.acquire(KEY))

    def test_hold_yields_none_when_busy(self):
        self.locks.acquire(KEY)
        with self.locks.hold(KEY) as handle:
            self.assertIsNone(handle)

    def test_corrupt_lock_record_is_ignored(self):
        self.storage.put_bytes(lock_key_for(KEY), b"not json at all")
        self.assertIsNone(self.locks.peek(KEY))
        self.assertIsNone(self.locks.acquire(KEY))

    def test_stats(self):
        handle = self.locks.acquire(KEY)
        self.locks.acquire(KEY)
        self.locks.release(handle)
        stats = self.locks.stats()
        self.assertEqual(stats["locks_acquired"], 1)
        self.assertEqual(stats["locks_busy"], 1)

    def test_lock_object_lands_under_the_locks_prefix(self):
        self.locks.acquire(KEY)
        self.assertEqual(self.storage.list("locks/"), [f"locks/{KEY}.lock"])


class S3LockTest(unittest.TestCase):
    """Two LockManagers on one bucket behave like two proxy instances."""

    @classmethod
    def setUpClass(cls):
        cls.s3 = FakeS3().start()

    @classmethod
    def tearDownClass(cls):
        cls.s3.stop()

    def make_storage(self, conditional=False):
        return S3Storage(
            S3Config(
                bucket=self.s3.bucket,
                region=self.s3.region,
                endpoint_url=self.s3.endpoint_url,
                access_key=self.s3.access_key,
                secret_key="secret-test",
                prefix="",
                path_style=True,
                conditional_locks=conditional,
                lock_verify_delay=0.05,
            )
        )

    def setUp(self):
        self.s3.objects.clear()
        self.s3.reset()
        self.clock = FakeClock()
        self.storage = self.make_storage()

    def test_two_instances_one_winner(self):
        a = LockManager(self.storage, ttl=60, clock=self.clock, instance="a")
        b = LockManager(self.storage, ttl=60, clock=self.clock, instance="b")
        winners = [manager.acquire(KEY) for manager in (a, b)]
        self.assertIsNotNone(winners[0])
        self.assertIsNone(winners[1])
        self.assertEqual(a.busy, 0)
        self.assertEqual(b.busy, 1)

    def test_parallel_acquire_has_exactly_one_winner(self):
        results = []
        barrier = threading.Barrier(4)

        def race(index):
            manager = LockManager(self.storage, ttl=60, clock=self.clock, instance=f"node-{index}")
            barrier.wait()
            results.append(manager.acquire(KEY))

        threads = [threading.Thread(target=race, args=(index,)) for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(1 for handle in results if handle is not None), 1, results)

    def test_stale_lock_is_broken_across_instances(self):
        a = LockManager(self.storage, ttl=30, clock=self.clock, instance="a")
        b = LockManager(self.storage, ttl=30, clock=self.clock, instance="b")
        self.assertIsNotNone(a.acquire(KEY))
        self.clock.advance(31)
        self.assertIsNotNone(b.acquire(KEY))
        self.assertEqual(b.broken, 1)

    def test_conditional_mode(self):
        storage = self.make_storage(conditional=True)
        a = LockManager(storage, ttl=60, clock=self.clock, instance="a")
        b = LockManager(storage, ttl=60, clock=self.clock, instance="b")
        self.assertIsNotNone(a.acquire(KEY))
        self.assertIsNone(b.acquire(KEY))
        self.assertTrue(a.release(a.peek(KEY)))

    def test_release_removes_the_lock_object(self):
        locks = LockManager(self.storage, ttl=60, clock=self.clock)
        handle = locks.acquire(KEY)
        self.assertIn(f"locks/{KEY}.lock", self.s3.keys())
        self.assertTrue(locks.release(handle))
        self.assertNotIn(f"locks/{KEY}.lock", self.s3.keys())
        self.assertEqual(self.s3.auth_errors, [])


class TieredLockBackendTest(unittest.TestCase):
    def test_locks_go_to_the_distributed_tier(self):
        with tempfile.TemporaryDirectory() as tmp:
            fs = FilesystemStorage(tmp)
            s3 = FakeS3().start()
            try:
                tiered = TieredStorage(
                    [
                        fs,
                        S3Storage(
                            S3Config(
                                bucket=s3.bucket,
                                region=s3.region,
                                endpoint_url=s3.endpoint_url,
                                access_key=s3.access_key,
                                secret_key="s",
                                prefix="",
                                path_style=True,
                                lock_verify_delay=0.0,
                            )
                        ),
                    ]
                )
                self.assertIs(tiered.lock_backend, tiered.backends[1])
                self.assertTrue(tiered.put_new("locks/x.lock", b"mine"))
                self.assertIn("locks/x.lock", s3.keys())
                self.assertEqual(fs.list("locks/"), [])
            finally:
                s3.stop()

    def test_local_only_tier_falls_back_to_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            fs = FilesystemStorage(tmp)
            tiered = TieredStorage([fs])
            self.assertIs(tiered.lock_backend, fs)
            self.assertTrue(tiered.put_new("locks/x.lock", b"mine"))
            self.assertFalse(tiered.put_new("locks/x.lock", b"theirs"))


class NullLockTest(unittest.TestCase):
    def test_everything_succeeds_without_storage(self):
        locks = NullLockManager(ttl=1)
        handle = locks.acquire(KEY)
        self.assertIsNotNone(handle)
        self.assertTrue(locks.refresh(handle))
        self.assertTrue(locks.wait_for_release(KEY))
        self.assertTrue(locks.release(handle))
        self.assertIsNone(locks.peek(KEY))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
