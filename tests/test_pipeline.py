"""Offline pipeline regression tests; no paid requests and no real Unity writes."""
import contextlib
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import pipeline
import hunyuan3d


def triangle_glb(path):
    data = struct.pack('<9f', 0, 0, 0, 1, 0, 0, 0, 1, 0)
    document = {'asset': {'version': '2.0'}, 'scene': 0,
                'scenes': [{'nodes': [0]}], 'nodes': [{'mesh': 0}],
                'meshes': [{'primitives': [{'attributes': {'POSITION': 0}}]}],
                'buffers': [{'byteLength': len(data)}],
                'bufferViews': [{'buffer': 0, 'byteOffset': 0, 'byteLength': len(data)}],
                'accessors': [{'bufferView': 0, 'componentType': 5126, 'count': 3,
                               'type': 'VEC3', 'min': [0, 0, 0], 'max': [1, 1, 0]}]}
    encoded = json.dumps(document).encode()
    encoded += b' ' * (-len(encoded) % 4)
    body = struct.pack('<I4s', len(encoded), b'JSON') + encoded + struct.pack('<I4s', len(data), b'BIN\x00') + data
    Path(path).write_bytes(struct.pack('<4sII', b'glTF', 2, 12 + len(body)) + body)
    return str(path)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'cropped.png'
        image = Image.new('RGBA', (128, 128), (17, 193, 85, 0))
        draw = ImageDraw.Draw(image)
        draw.rectangle((25, 10, 42, 113), fill=(245, 245, 245, 255))
        draw.rectangle((75, 10, 92, 113), fill=(210, 130, 40, 255))
        image.save(self.source)
        self.dest = self.root / 'assets' / 'Bamboo Ladder'

    def invoke(self, extra=(), export_error=None, split=False):
        argv = ['pipeline.py', '--input', str(self.source), '--work-dir', str(self.root / 'assets'),
                '--name', 'Bamboo Ladder', '--size', '128', '--poll-interval', '0', '--token', 'test-secret', *([] if split else ['--no-split']), *extra]
        def download(result, prefix):
            return [triangle_glb(prefix + '.glb')]
        with patch.object(sys, 'argv', argv), patch.object(hunyuan3d, 'submit', side_effect=['job-one', 'job-two']), \
                patch.object(hunyuan3d, 'query', return_value={'Status': 'DONE', 'ResultCreditConsumed': 30}), \
                patch.object(hunyuan3d, 'download', side_effect=download), \
                patch.object(pipeline.unity_export, 'preflight', return_value={'enabled': False}) as preflight, \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            if export_error:
                with patch.object(pipeline.unity_export, 'export_assets', side_effect=export_error):
                    code = pipeline.main()
            else:
                code = pipeline.main()
        manifest = json.loads((self.dest / 'manifest.json').read_text(encoding='utf-8'))
        return code, manifest, json.loads(stdout.getvalue()), preflight

    def test_single_prop_preserves_icon_and_metadata_after_cleanup(self):
        code, manifest, output, _ = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(manifest['bg_removal']['method'], 'alpha')
        self.assertFalse(manifest['prepared_dir'])
        self.assertEqual(len(manifest['parts']), 1)
        record = manifest['parts'][0]
        icon = Image.open(self.dest / record['icon'])
        self.assertEqual(icon.mode, 'RGBA')
        self.assertEqual(icon.getchannel('A').getextrema(), (0, 255))
        self.assertTrue((self.dest / 'Bamboo Ladder.glb').is_file())
        self.assertTrue((self.dest / 'Bamboo Ladder.prop.json').is_file())
        self.assertNotIn('test-secret', json.dumps(manifest))

    def test_dry_run_never_calls_unity_preflight_or_export(self):
        with patch.object(pipeline.unity_export, 'export_assets') as export:
            code, manifest, output, preflight = self.invoke(['--dry-run', '--unity-copy'])
        self.assertEqual(code, 0)
        preflight.assert_not_called()
        export.assert_not_called()
        self.assertTrue(Path(manifest['prepared_dir']).is_dir())
        self.assertFalse((self.dest / 'Bamboo Ladder.glb').exists())

    def test_export_failure_retains_model_icon_and_prepared_recovery(self):
        code, manifest, output, _ = self.invoke(['--unity-copy'], OSError('copy unavailable'))
        self.assertEqual(code, 1)
        self.assertEqual(manifest['status'], 'export_failed')
        self.assertTrue(Path(manifest['prepared_dir']).is_dir())
        self.assertTrue((self.dest / manifest['parts'][0]['icon']).is_file())
        self.assertTrue((self.dest / 'Bamboo Ladder.glb').is_file())
        self.assertIn('copy unavailable', manifest['unity']['error'])

    def test_previous_intermediate_inputs_are_not_deleted(self):
        previous = self.dest / '_prepared_previous' / 'cutouts'
        previous.mkdir(parents=True)
        original_bytes = self.source.read_bytes()
        self.source = previous / 'source.png'
        self.source.write_bytes(original_bytes)
        self.invoke(['--dry-run', '--allow-degraded'])
        self.assertTrue(self.source.exists())

    def test_actual_lowpoly_model_recorded(self):
        code, manifest, _, _ = self.invoke(['--model', '3.1'])
        self.assertEqual(manifest['generation']['model'], '3.0')

    def test_no_icon_option_preserved(self):
        code, manifest, _, _ = self.invoke(['--no-icon'])
        self.assertEqual(code, 0)
        self.assertNotIn('icon', manifest['parts'][0])
        self.assertFalse((self.dest / 'Bamboo Ladder.png').exists())

    def test_stdin_credential_takes_priority(self):
        with patch.object(sys, 'stdin', io.StringIO('stdin-secret\n')), \
                patch.object(pipeline, 'generate', return_value=([], [])) as generate:
            code, manifest, _, _ = self.invoke(['--token-stdin'])
        self.assertEqual(code, 0)
        self.assertEqual(generate.call_args.args[-1], 'stdin-secret')
        self.assertNotIn('stdin-secret', json.dumps(manifest))

    def test_dry_run_never_reads_stdin_credential(self):
        with patch.object(sys.stdin, 'read', side_effect=AssertionError('must not wait for token')):
            code, _, _, _ = self.invoke(['--dry-run', '--token-stdin'])
        self.assertEqual(code, 0)

    def test_multiple_models_keep_their_own_icons(self):
        code, manifest, _, _ = self.invoke(split=True)
        self.assertEqual(code, 0)
        self.assertEqual([p['part'] for p in manifest['parts']], [0, 1])
        for part in manifest['parts']:
            model = next(f for f in part['files'] if f.endswith('.glb'))
            self.assertEqual(Path(model).stem, Path(part['icon']).stem)
        first = Image.open(self.dest / manifest['parts'][0]['icon'])
        second = Image.open(self.dest / manifest['parts'][1]['icon'])
        self.assertNotEqual(first.getpixel((64, 64)), second.getpixel((64, 64)))


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.args = SimpleNamespace(max_wait=10, prompt=None, model='3.0', generate_type='LowPoly',
                                    face_count=30000, result_format=None, enable_pbr=False, poll_interval=0)
        self.parts = [{'file': 'part-a'}, {'file': 'part-b'}, {'file': 'part-c'}]
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_quota_failure_accounts_for_unsubmitted_parts(self):
        with patch.object(hunyuan3d, 'submit', side_effect=RuntimeError('daily submit limit exceeded')):
            results, failed = pipeline.generate(self.parts, self.temp.name, 'prop', self.args, 'token')
        self.assertEqual(results, [])
        self.assertEqual({r['part'] for r in failed}, {0, 1, 2})

    def test_download_failure_is_recorded_and_other_job_survives(self):
        def download(result, prefix):
            if result['job'] == 'bad':
                raise OSError('download interrupted')
            return [triangle_glb(prefix + '.glb')]
        with patch.object(hunyuan3d, 'submit', side_effect=['bad', 'good']), \
                patch.object(hunyuan3d, 'query', side_effect=lambda job, token: {'Status': 'DONE', 'job': job}), \
                patch.object(hunyuan3d, 'download', side_effect=download):
            results, failed = pipeline.generate(self.parts[:2], self.temp.name, 'prop', self.args, 'token')
        self.assertEqual([r['part'] for r in results], [1])
        self.assertEqual([r['part'] for r in failed], [0])
        self.assertTrue((Path(self.temp.name) / 'prop_part01.glb').exists())

    def test_empty_response_does_not_accept_stale_glb(self):
        triangle_glb(Path(self.temp.name) / 'prop.glb')
        with patch.object(hunyuan3d, 'submit', return_value='job'), \
                patch.object(hunyuan3d, 'query', return_value={'Status': 'DONE'}), \
                patch.object(hunyuan3d, 'download', return_value=[]):
            results, failed = pipeline.generate(self.parts[:1], self.temp.name, 'prop', self.args, 'token')
        self.assertFalse(results)
        self.assertEqual(len(failed), 1)

    def test_timeout_reports_running_and_pending_jobs(self):
        with patch.object(pipeline.time, 'time', side_effect=[0, 0, 100]), \
                patch.object(hunyuan3d, 'submit', side_effect=['job-a', 'job-b']), \
                patch.object(hunyuan3d, 'query', return_value={'Status': 'RUN'}):
            results, failed = pipeline.generate(self.parts, self.temp.name, 'prop', self.args, 'token')
        self.assertEqual({r['part'] for r in failed}, {0, 1, 2})
        self.assertEqual({r.get('job_id') for r in failed}, {'job-a', 'job-b', None})

    def test_extra_output_format_compatibility(self):
        self.args.result_format = 'FBX'
        def download(result, prefix):
            Path(prefix + '.fbx').write_bytes(b'FBX test fixture')
            return [prefix + '.fbx']
        with patch.object(hunyuan3d, 'submit', return_value='job'), \
                patch.object(hunyuan3d, 'query', return_value={'Status': 'DONE'}), \
                patch.object(hunyuan3d, 'download', side_effect=download):
            results, failed = pipeline.generate(self.parts[:1], self.temp.name, 'prop', self.args, 'token')
        self.assertFalse(failed)
        self.assertEqual(results[0]['files'], ['prop.fbx'])


class DownloadTests(unittest.TestCase):
    def test_standalone_submit_accepts_stdin_credential(self):
        with patch.object(sys, 'argv', ['hunyuan3d.py', 'submit', '--prompt', 'wooden bench', '--token-stdin']), \
                patch.object(sys, 'stdin', io.StringIO('stdin-secret\n')), \
                patch.object(hunyuan3d, 'submit', return_value='job') as submit, \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            hunyuan3d.main()
        self.assertEqual(submit.call_args.args[0], 'stdin-secret')
        self.assertNotIn('stdin-secret', stdout.getvalue())

    def test_preview_failure_keeps_model(self):
        with tempfile.TemporaryDirectory() as folder:
            def fetch(url, path):
                if url == 'preview':
                    raise OSError('optional preview failed')
                Path(path).write_bytes(b'model')
            with patch.object(hunyuan3d, '_download_file', side_effect=fetch):
                files = hunyuan3d.download({'ResultFile3Ds': [{'Type': 'GLB', 'Url': 'model', 'PreviewImageUrl': 'preview'}]}, str(Path(folder) / 'prop'))
            self.assertEqual([Path(p).suffix for p in files], ['.glb'])

    def test_base64_size_checked_after_encoding(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'large.png'
            path.write_bytes(b'0' * (5 * 1024 * 1024))
            with self.assertRaisesRegex(ValueError, 'base64'):
                hunyuan3d.build_body(image=str(path))

    def test_invalid_output_type_cannot_create_path(self):
        with self.assertRaises(ValueError):
            hunyuan3d.download({'ResultFile3Ds': [{'Type': '../../bad', 'Url': 'unused'}]}, 'prop')


if __name__ == '__main__':
    unittest.main()
