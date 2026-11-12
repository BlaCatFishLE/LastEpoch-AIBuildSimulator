"""R4: cleanup must reclassify after acquiring the original writer's lock."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from le_calc.workflow import atomic_io, commit


class PointerCleanupRaceTest(unittest.TestCase):
    def test_dataset_cleanup_after_publish(self):
        self._check('data/current.json', 'dataset_id')

    def test_build_cleanup_after_publish(self):
        self._check('data/builds/letools/probe/current.json', 'revision_id')

    def _check(self, target, key):
        with tempfile.TemporaryDirectory(prefix='p5-cleanup-race-') as temp:
            root = Path(temp)
            pointer = root / target
            pointer.parent.mkdir(parents=True)
            pointer.write_text(json.dumps({key: 'old'}), encoding='utf8')
            a, b = root / 'a.json', root / 'b.json'
            a.write_text(json.dumps({key: 'A'}), encoding='utf8')
            b.write_text(json.dumps({key: 'B'}), encoding='utf8')
            old_sha = atomic_io.sha256_file(pointer)
            ops = root / 'ops'
            ready, publish, done = (threading.Event() for _ in range(3))
            results, errors = {}, []
            write_intent = commit._write_intent
            write_record = commit._write_operation_record
            acquire = commit.OperationLock.acquire

            def update(candidate, op, sha):
                return commit.update_pointer(
                    candidate, target, operation_id=op, attempt_id='one',
                    input_fingerprint=commit.compute_input_fingerprint({'op': op}),
                    expected_current_sha256=sha, root=root, operations_root=ops)

            def intent_hook(*args, **kwargs):
                result = write_intent(*args, **kwargs)
                if threading.current_thread().name == 'writer-A':
                    ready.set()
                    if not publish.wait(10):
                        raise TimeoutError('cleanup not reached')
                return result

            def record_hook(op, *args, **kwargs):
                if op == 'op-a':
                    raise OSError('injected record failure')
                return write_record(op, *args, **kwargs)

            def acquire_hook(lock):
                if threading.current_thread().name == 'writer-B' and lock.operation_id == 'op-a':
                    publish.set()
                    if not done.wait(10):
                        raise TimeoutError('A did not finish')
                return acquire(lock)

            def worker(label, candidate, op):
                try:
                    if label == 'B' and not ready.wait(10):
                        raise TimeoutError('intent missing')
                    results[label] = update(candidate, op, old_sha)
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    if label == 'A':
                        done.set()

            with patch.object(commit, '_write_intent', side_effect=intent_hook), \
                 patch.object(commit, '_write_operation_record', side_effect=record_hook), \
                 patch.object(commit.OperationLock, 'acquire', acquire_hook):
                threads = [threading.Thread(target=worker, name='writer-' + label,
                           args=(label, candidate, op))
                           for label, candidate, op in [('A', a, 'op-a'), ('B', b, 'op-b')]]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(20)
                self.assertFalse(any(t.is_alive() for t in threads))
            self.assertEqual(errors, [])
            self.assertEqual(results['A'].issues[0]['code'], 'record_write_failed')
            self.assertEqual(results['B'].status, 'failed')
            self.assertEqual(results['B'].issues[0]['code'], 'pending_pointer_recovery')
            self.assertTrue(results['B'].issues[0]['retryable'])
            self.assertTrue(commit._intent_path(ops, 'op-a').is_file())
            self.assertIsNone(commit.read_operation_record('op-b', ops))
            self.assertFalse(commit._intent_path(ops, 'op-b').exists())
            self.assertEqual(json.loads(pointer.read_text()), {key: 'A'})
            retry = update(a, 'op-a', old_sha)
            self.assertEqual(retry.status, 'replayed', retry.to_dict())
            self.assertFalse(commit._intent_path(ops, 'op-a').exists())
            self.assertIsNotNone(commit.read_operation_record('op-a', ops))
            advance = update(b, 'op-b', atomic_io.sha256_file(pointer))
            self.assertEqual(advance.status, 'committed', advance.to_dict())
            self.assertEqual(json.loads(pointer.read_text()), {key: 'B'})
