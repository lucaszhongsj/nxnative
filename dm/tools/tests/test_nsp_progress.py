"""NSP script progress (scripts/build_ipa_from_nsp.py --progress): the json mode's events, a count held back and sent by the ticker, a failed step's log tail,
and the human modes' lines; no game files, keys or Xcode needed (temporary files only in a temporary folder)."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[3] / 'scripts/build_ipa_from_nsp.py'
spec = importlib.util.spec_from_file_location('build_ipa_from_nsp', SCRIPT)
nsp = importlib.util.module_from_spec(spec); spec.loader.exec_module(nsp)

class OneTick:
    """A ticker stop event that lets one tick through"""
    def __init__(self): self.n = 0
    def wait(self, timeout): self.n += 1; return self.n > 1
    def is_set(self): return False

class Json(unittest.TestCase):
    def setUp(self):
        self.now, self.out = 100.0, io.StringIO()
        self.prog = nsp.Progress('json')
        for cm in (contextlib.redirect_stdout(self.out), mock.patch.object(nsp.time, 'monotonic', side_effect=lambda: self.now),
                   mock.patch.object(nsp.threading, 'Thread'), mock.patch.object(nsp, 'PROG', self.prog)):   # no real ticker: the tests tick it themselves
            cm.__enter__(); self.addCleanup(cm.__exit__, None, None, None)

    def events(self, ev=None):
        return [e for e in map(json.loads, self.out.getvalue().splitlines()) if ev in (None, e['ev'])]

    def test_warned_stage_end(self):   # a stage that finished without what it makes (the pipeline archive): ok, with a warn flag the launcher can show
        self.prog.plan(2)
        self.prog.stage('Pipeline archive'); self.prog.end('failed, the app is packed without one', warn=True)
        self.prog.stage('pack'); self.prog.end('done')
        e = self.events('stage_end')
        self.assertEqual([(x['ok'], x.get('warn')) for x in e], [(True, True), (True, None)])

    def test_events(self):
        self.prog.plan(2)
        self.prog.stage('extract Pokémon', step='reading', done=0, total=10, unit='files', est=True)
        self.prog.set(step='writing', done=2, total=4)
        self.prog.say('[RED] two\nlines')
        self.prog.end('4 files')
        self.prog.stage('rewrite')
        self.prog.close(ok=False); self.prog.close(ok=False)
        es = self.events()
        self.assertEqual([e['ev'] for e in es], ['stage', 'progress', 'step', 'progress', 'say', 'stage_end', 'stage', 'progress', 'stage_end'])
        self.assertEqual([es[0][k] for k in ('k', 'n', 'name', 'step')], [1, 2, 'extract Pokémon', 'reading'])
        self.assertEqual([es[1][k] for k in ('frac', 'est', 'eta')], [0, True, None])
        self.assertEqual([es[3][k] for k in ('step', 'frac', 'unit', 'est')], ['writing', 0.5, '', False])   # a new step starts with no count
        self.assertEqual(es[4]['msg'], '[RED] two\nlines')
        self.assertEqual([es[5][k] for k in ('ok', 'summary')], [True, '4 files'])
        self.assertEqual([es[7][k] for k in ('done', 'total', 'frac')], [None, None, None])
        self.assertFalse(es[8]['ok'])
        self.assertIn('Pokémon', self.out.getvalue())   # not escaped

    def test_count_held_back(self):
        self.prog.stage('rewrite', done=0, total=3, unit='modules')
        self.now += 1; self.prog.set(done=1)
        self.now += 0.1; self.prog.set(done=2)   # within JSON_INTERVAL of the last event
        self.prog._tick(OneTick())
        self.assertEqual([e['done'] for e in self.events('progress')], [0, 1])
        self.now += 0.5; self.prog._tick(OneTick())   # the ticker sends it
        self.assertEqual([e['done'] for e in self.events('progress')], [0, 1, 2])
        self.prog.set(done=3); self.prog.end()   # held back again, then sent before stage_end
        es = self.events()
        self.assertEqual([es[-2][k] for k in ('done', 'frac')], [3, 0.99])
        self.assertEqual(es[-1]['ev'], 'stage_end')

    def test_no_prompt(self):
        with mock.patch.object(nsp, 'in_ipython', return_value=True), mock.patch('builtins.input') as inp:
            with self.assertRaisesRegex(nsp.BuildError, 'does not prompt'): nsp.ask('NSP folder: ', 'the NSP folder')
        inp.assert_not_called()

    def test_failed_step(self):
        self.prog.stage('child')
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'child.log'
            with self.assertRaisesRegex(nsp.BuildError, 'exit code 7'):
                nsp.run([sys.executable, '-c', 'import sys; print("out"); print("err", file=sys.stderr); sys.exit(7)'], log)
            self.assertEqual(sorted(log.read_text().split()), ['err', 'out'])
        es = self.events()
        self.assertEqual([e['ev'] for e in es], ['stage', 'progress', 'stage_end', 'say'])
        self.assertFalse(es[2]['ok'])
        self.assertTrue('out' in es[3]['msg'] and 'err' in es[3]['msg'])

    def test_game(self):   # which game the app is (issue #1627): the event, then the message for people
        app, icon = Path('/b/Zelda.app'), Path('/b/x/extract/v131072/icon.jpg')
        nsp.say_game(0x0400486024DEC000, 'The Legend of Zelda', '1.0', 'org.nxnative.0400486024dec000', app, icon)
        nsp.say_game(0x0400486024DEC000, 'Zelda', '1.0', 'b', None, None)   # an Xcode project, a game without an icon
        es = self.events()
        self.assertEqual([e['ev'] for e in es], ['game', 'say', 'game', 'say'])
        self.assertEqual({k: v for k, v in es[0].items() if k != 't'}, {'ev': 'game', 'title_id': '0400486024DEC000', 'name': 'The Legend of Zelda', 'version': '1.0',
                                                                   'bundle_id': 'org.nxnative.0400486024dec000', 'out': str(app), 'icon': str(icon)})
        self.assertIn(f'; app: {app}', es[1]['msg'])
        self.assertEqual((es[2]['out'], es[2]['icon']), (None, None))
        self.assertNotIn('; app:', es[3]['msg'])
        self.prog.mode = 'lines'; nsp.say_game(1, 'Zelda', '1.0', 'b', Path('/b/z.ipa'), None)   # the human modes get only the message
        self.assertNotIn('"ev"', self.out.getvalue().splitlines()[-1])

class ShadersStale(unittest.TestCase):
    """build_shaders tells a front end (event shaders_stale, json mode) that cached libraries cannot be reused, once, before it rebuilds them"""
    class Stop(Exception): pass

    def run_step(self, mode, chain_dir=None, digest='aaaa', stamp=None, caches=None, keys=('x',), mac=True, reuse=False):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            sh = Path(tmp) / 'sh'
            if caches is None: caches = [('cache', chain_dir or 'b-OLD', ('x',), stamp)]
            for target, chain, cached, inputs in caches:
                d = sh / target / chain; d.mkdir(parents=True)
                for key in cached: (d / f'{key}_b.metallib').write_text('')
                if inputs: (d / 'inputs.tsv').write_text(f'game\tg\ninputs\t{inputs}\n')
            (Path(tmp) / 'shader_map.tsv').write_text(''.join(f'p.bin\t{i}\tvs\t{k}\tsrc\n' for i, k in enumerate(keys)))
            prog = nsp.Progress(mode)
            g = mock.Mock(); g.dir = Path(tmp); g.table.side_effect = lambda name: Path(tmp) / name
            done = subprocess.CompletedProcess([], 0, stdout=f'{digest}\n', stderr='')
            with contextlib.redirect_stdout(out), mock.patch.object(nsp, 'PROG', prog), mock.patch.object(nsp.threading, 'Thread'), \
                 mock.patch.object(nsp, 'chain_of', return_value='NEW'), mock.patch.object(nsp.subprocess, 'run', return_value=done), \
                 mock.patch.object(nsp, 'run', side_effect=self.Stop) as build, mock.patch.object(nsp, 'rmtree'), \
                 mock.patch.object(nsp, 'fingerprint', return_value='K'), mock.patch.object(nsp, 'reused', return_value='ready' if reuse else None), \
                 mock.patch.object(nsp, 'chain_tables', return_value=True):
                args = ('py', Path(tmp), sh, Path(tmp) / 'xlate', {'WORK': tmp}, Path(tmp), g)
                if reuse:
                    nsp.build_shaders(*args, pkg_key='P', mac=mac)
                    build.assert_not_called()
                else:
                    with self.assertRaises(self.Stop): nsp.build_shaders(*args, mac=mac)
        return out.getvalue()

    def events(self, text): return [e for e in (json.loads(l) for l in text.splitlines()) if e['ev'] == 'shaders_stale']

    def test_chain_changed(self):
        es = self.events(self.run_step('json'))
        self.assertEqual(len(es), 1)
        self.assertEqual([es[0][k] for k in ('reason', 'old', 'new', 'count')], ['chain', 'b-OLD', 'b-NEW', 1])

    def test_inputs_changed(self):
        es = self.events(self.run_step('json', chain_dir='b-NEW', digest='bbbb', stamp='aaaa'))
        self.assertEqual([es[0][k] for k in ('reason', 'old', 'new', 'count')], ['inputs', 'aaaa', 'bbbb', 1])

    def test_current_has_no_event(self):
        self.assertEqual(self.events(self.run_step('json', chain_dir='b-NEW', digest='aaaa', stamp='aaaa')), [])

    def test_partial_current_chain_still_reports_stale(self):
        caches = [('cache', 'b-OLD', ('a', 'b'), 'aaaa'), ('cache', 'b-NEW', ('a',), None)]
        es = self.events(self.run_step('json', caches=caches, keys=('a', 'b')))
        self.assertEqual(len(es), 1)
        self.assertEqual([es[0][k] for k in ('reason', 'old', 'new', 'count')], ['chain', 'b-OLD', 'b-NEW', 2])

    def test_current_mac_does_not_hide_stale_ios(self):
        caches = [('cache', 'b-NEW', ('x',), 'aaaa'), ('ios', 'b-OLD', ('x',), 'aaaa')]
        es = self.events(self.run_step('json', caches=caches, mac=False))
        self.assertEqual(len(es), 1)
        self.assertEqual([es[0][k] for k in ('reason', 'count')], ['chain', 2])

    def test_count_uses_current_keys_not_historical_copies(self):
        caches = [('cache', 'b-OLD', ('a', 'removed'), 'aaaa'), ('cache', 'b-OLDER', ('a',), 'aaaa')]
        es = self.events(self.run_step('json', caches=caches, keys=('a', 'b', 'b')))
        self.assertEqual(es[0]['count'], 2)   # a and b, once each; removed is no longer built

    def test_count_includes_both_selected_targets(self):
        caches = [('cache', 'b-NEW', ('x',), 'aaaa'), ('ios', 'b-NEW', ('x',), 'aaaa')]
        es = self.events(self.run_step('json', caches=caches, digest='bbbb', mac=False))
        self.assertEqual([es[0][k] for k in ('reason', 'count')], ['inputs', 2])

    def test_ios_only_count_excludes_old_mac_libraries(self):
        caches = [('cache', 'b-OLD', ('x',), 'aaaa'), ('ios', 'b-OLD', ('x',), 'aaaa')]
        es = self.events(self.run_step('json', caches=caches, mac=False))
        self.assertEqual(es[0]['count'], 1)

    def test_unselected_mac_stamp_has_no_event(self):
        caches = [('cache', 'b-NEW', (), 'old'), ('ios', 'b-NEW', ('x',), 'aaaa')]
        self.assertEqual(self.events(self.run_step('json', caches=caches, mac=False)), [])

    def test_completed_current_chain_ignores_old_folders(self):
        caches = [('cache', 'b-OLD', ('x',), 'old'), ('cache', 'b-NEW', ('x',), 'aaaa')]
        self.assertEqual(self.events(self.run_step('json', caches=caches)), [])

    def test_no_cached_libraries_has_no_event(self):
        self.assertEqual(self.events(self.run_step('json', caches=[])), [])

    def test_reused_libraries_have_no_event(self):
        self.assertEqual(self.events(self.run_step('json', chain_dir='b-NEW', stamp='aaaa', reuse=True)), [])

    def test_event_precedes_rebuild_with_same_message(self):
        es = [json.loads(line) for line in self.run_step('json').splitlines()]
        stale = next(i for i, e in enumerate(es) if e['ev'] == 'shaders_stale')
        self.assertIsInstance(es[stale]['t'], (int, float))
        self.assertEqual(es[stale + 1]['ev'], 'say')
        self.assertIn('[NOTE] cached shader libraries cannot be reused', es[stale + 1]['msg'])

    def test_no_json_unless_asked(self):
        for mode in ('bar', 'lines', 'off'):
            with self.subTest(mode=mode):
                out = self.run_step(mode)
                self.assertNotIn('shaders_stale', out)
                self.assertIn('[NOTE] cached shader libraries cannot be reused (b-OLD -> b-NEW)', out)


class Cli(unittest.TestCase):
    def test_argument_error_precedes_json_mode(self):
        r = subprocess.run([sys.executable, '-B', str(SCRIPT), '--progress', 'json', '--invalid-option'],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual((r.returncode, r.stdout), (2, ''))
        self.assertIn('usage:', r.stderr)
        self.assertIn('unrecognized arguments: --invalid-option', r.stderr)

    def test_json_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run([sys.executable, '-B', str(SCRIPT), '--progress', 'json', str(Path(tmp) / 'missing'), '-k', str(Path(tmp) / 'keys')],
                               capture_output=True, text=True, timeout=30)
        self.assertEqual((r.returncode, r.stderr), (1, ''))
        es = [json.loads(l) for l in r.stdout.splitlines()]
        self.assertTrue(es and all(e['ev'] == 'say' for e in es))
        self.assertIn('[RED] not a folder:', es[-1]['msg'])

class HumanModes(unittest.TestCase):
    def test_lines(self):
        for mode in ('bar', 'lines', 'off'):
            with self.subTest(mode=mode), contextlib.redirect_stdout(io.StringIO()) as out:
                prog = nsp.Progress(mode); prog.plan(1)
                prog.stage('compile', done=0, total=10); ticker = prog.ticker
                prog.set(step='writing', done=1); prog.say('message'); prog.close()
                if ticker: ticker.join(2); self.assertFalse(ticker.is_alive())
            text = out.getvalue()
            self.assertIn('message', text)
            self.assertIn('✓ [1/1] compile', text)
            with contextlib.redirect_stdout(io.StringIO()) as out2:
                prog = nsp.Progress(mode); prog.plan(1); prog.stage('archive'); prog.end('failed', warn=True); prog.close()
            self.assertIn('⚠ [1/1] archive', out2.getvalue())
            self.assertEqual('\r' in text, mode == 'bar')
            self.assertEqual(ticker is None, mode == 'off')


class ShellFrameworkProgress(unittest.TestCase):
    """Progress of --shell-framework (stage 'game framework') and IpaWatch on directory or None targets."""

    def setUp(self):
        self.now, self.wall, self.out = 100.0, 1_700_000_000.0, io.StringIO()
        self.prog = nsp.Progress('json')
        for cm in (
            contextlib.redirect_stdout(self.out),
            mock.patch.object(nsp.time, 'monotonic', side_effect=lambda: self.now),
            mock.patch.object(nsp.time, 'time', side_effect=lambda: self.wall),
            mock.patch.object(nsp.threading, 'Thread'),
            mock.patch.object(nsp, 'PROG', self.prog),
        ):
            cm.__enter__()
            self.addCleanup(cm.__exit__, None, None, None)

    def events(self, ev=None):
        return [e for e in map(json.loads, self.out.getvalue().splitlines()) if ev in (None, e['ev'])]

    def write_pso_script(self, pso_work, bases, computes=0, mtime=None):
        pso_work.mkdir(parents=True, exist_ok=True)
        render = [{'vertex_function_descriptor': f'fnd:v{i}', 'fragment_function_descriptor': f'fnd:f{i}'} for i in range(bases)]
        desc = {'render_pipeline_descriptors': render}
        if computes:
            desc['compute_pipeline_descriptors'] = [{'compute_function_descriptor': f'fnd:c{i}'} for i in range(computes)]
        js = pso_work / 'bases.mtl4-json'
        js.write_text(json.dumps({'pipeline_descriptors': desc}))
        if mtime is not None:
            nsp.os.utime(js, (mtime, mtime))
        return js

    def mark_batch(self, batches_dir, name, arch, done, mtime=None):
        gdir = batches_dir / name
        gdir.mkdir(parents=True, exist_ok=True)
        part = gdir / f'{arch}.mtl4a.partial'
        dst = gdir / f'{arch}.mtl4a'
        kf = gdir / f'key.{arch}'
        if done:
            part.unlink(missing_ok=True)
            dst.write_bytes(b'archive')
            kf.write_text('digest\n')
            targets = (gdir, dst, kf)
        else:
            kf.unlink(missing_ok=True)
            part.write_bytes(b'compiling')
            targets = (gdir, part)
        if mtime is not None:
            for p in targets:
                nsp.os.utime(p, (mtime, mtime))

    def test_watch_directory_and_none_targets_are_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shell_dir = root / 'frameworks'
            shell_dir.mkdir()
            existing_fw = shell_dir / 'DmGame01007EF00011E000.framework'
            existing_fw.mkdir()
            missing_fw = shell_dir / 'DmGame0100000000010000.framework'
            for target in (None, shell_dir, existing_fw, missing_fw):
                with self.subTest(target=target):
                    watch = nsp.IpaWatch(SCRIPT.parents[1], root / 'ipa', target)
                    self.assertIsNone(watch.part)
                    self.assertIsNone(watch.last)
                    self.assertIsNone(watch.poll())
                    watch.line('[12:00:00] pipeline archive for g18p (metal-tt)')
                    self.assertIsNone(watch.poll())
                    watch.line('[12:00:01] xcodebuild (Release, unsigned, the game framework DmGame01007EF00011E000 with its own host)')
                    self.assertIsNone(watch.poll())
                    watch.state = 'pack'
                    self.assertIsNone(watch.poll())

    def test_poll_pso_work_batches_and_stale_guard(self):
        arch = 'applegpu_g18p'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ipa_work = root / 'ipa'
            pso_work = ipa_work / 'build/ios/pso/work'
            batches = pso_work / f'batches.{arch}'
            stale = self.wall - 30.0
            self.write_pso_script(pso_work, bases=2500, computes=20, mtime=stale)
            self.mark_batch(batches, '00001', arch, done=True, mtime=stale)
            self.mark_batch(batches, '00002', arch, done=True, mtime=stale)
            nsp.os.utime(batches, (stale, stale))

            watch = nsp.IpaWatch(SCRIPT.parents[1], ipa_work, root / 'DmGame01007EF00011E000.framework')
            watch.line('[12:00:00] pipeline archive for g18p (metal-tt)')
            self.assertIsNone(watch.poll())

            # Batch 1 starts compiling now while batch 2 still holds files from an interrupted earlier run.
            self.mark_batch(batches, '00001', arch, done=False, mtime=self.wall)
            self.assertEqual(watch.poll(), {'done': 0, 'total': 5, 'unit': 'batches', 'note': ''})

            # Batch 1 finishes and batch 2 starts compiling: only sequential base groups before the active one count.
            self.mark_batch(batches, '00001', arch, done=True, mtime=self.wall)
            self.mark_batch(batches, '00002', arch, done=False, mtime=self.wall)
            self.assertEqual(watch.poll(), {'done': 1, 'total': 5, 'unit': 'batches', 'note': ''})

            # All three render-base groups finish, and compute batch c00002 finishes before c00001.
            self.mark_batch(batches, '00002', arch, done=True, mtime=self.wall)
            self.mark_batch(batches, '00003', arch, done=True, mtime=self.wall)
            self.mark_batch(batches, 'c00001', arch, done=False, mtime=self.wall)
            self.mark_batch(batches, 'c00002', arch, done=True, mtime=self.wall)
            self.assertEqual(watch.poll(), {'done': 4, 'total': 5, 'unit': 'batches', 'note': ''})

            # Final compute batch finishes and metal-pack starts merging.
            self.mark_batch(batches, 'c00001', arch, done=True, mtime=self.wall)
            pack_log = pso_work / f'metal-pack.{arch}.log'
            pack_log.write_text('')
            nsp.os.utime(pack_log, (self.wall, self.wall))
            self.assertEqual(watch.poll(), {'done': 5, 'total': 5, 'unit': 'batches', 'note': 'merging'})

            # A small game that fits in one compiler run (total <= 1) uses no batch count.
            sh_work = root / 'small'
            self.write_pso_script(sh_work / 'build/ios/pso/work', bases=500, computes=0, mtime=self.wall)
            small_watch = nsp.IpaWatch(SCRIPT.parents[1], sh_work, None)
            small_watch.line('[12:00:00] pipeline archive for g18p (metal-tt)')
            self.assertIsNone(small_watch.poll())

    def test_framework_lines_and_json_progress_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / 'repo'
            gen_src = repo / 'dm/ios/Gen/src'
            gen_src.mkdir(parents=True)
            for idx in range(3):
                (gen_src / f'u{idx}.c').write_text('')
            ipa_work = root / 'ipa'
            pkg = ipa_work / 'build/ios/pkg'
            pkg.mkdir(parents=True)
            fw_dir = root / 'shell/DmGame01007EF00011E000.framework'

            watch = nsp.IpaWatch(repo, ipa_work, fw_dir)
            self.prog.plan(1)
            self.prog.stage('game framework', poll=watch.poll, step='starting')
            watch.line('[12:00:00] generate Gen/ (01007EF00011E000, modules: main sdk, graphics: nvn)')
            watch.line('[12:00:01] pipeline archive for g18p (metal-tt)')
            watch.line('[12:00:02] pso_offline: applegpu_g18p: metal-tt, 2100 bases, 32 compute pipelines (minutes) ...')
            watch.line('[12:00:03] pso_offline: applegpu_g18p: batch 1/5: 1000 bases, compiling or checking cache')
            self.now += 1.0
            self.wall += 4.0
            watch.line('[12:00:07] pso_offline: applegpu_g18p: batch 1/5: ready (reused)')
            self.now += 1.0
            self.wall += 4.0
            watch.line('[12:00:11] pso_offline: applegpu_g18p: batch 2/5: ready (compiled)')
            self.now += 1.0
            self.wall += 4.0
            watch.line('[12:00:15] pso_offline: applegpu_g18p: batch 3/5: ready (compiled)')
            self.now += 1.0
            self.wall += 4.0
            # Out-of-order parallel compute completion: batch 5 finishes before batch 4 starts logging.
            watch.line('[12:00:19] pso_offline: applegpu_g18p: batch 5/5: ready (compiled)')
            self.now += 1.0
            self.wall += 1.0
            watch.line('[12:00:20] pso_offline: applegpu_g18p: batch 4/5: 16 compute pipelines, compiling or checking cache')
            self.now += 1.0
            self.wall += 2.0
            watch.line('[12:00:22] pso_offline: applegpu_g18p: batch 4/5: ready (compiled)')
            watch.line('[12:00:23] pso_offline: applegpu_g18p: merging 5 batches')
            watch.line('[12:00:30] framework: Gen/framework (modules main sdk, profile.tsv, game.manifest.json)')
            watch.line('[12:00:31] xcodebuild (Release, unsigned, the game framework DmGame01007EF00011E000 with its own host)')

            flog = pkg / 'build-framework.log'
            flog.write_bytes(b'CompileC a.o a.c\nCompileC b.o b.c\nSwiftCompile normal arm64 GameMenu.swift\nLd DmGame normal arm64\n')
            nsp.os.utime(flog, (self.wall, self.wall))
            polled = watch.poll()
            self.assertEqual(polled, {'done': 2, 'note': 'linking'})
            assert polled is not None
            self.now += 1.0
            self.prog.set(**polled)
            flog.write_bytes(flog.read_bytes() + b'CompileC c.o c.c\n')
            nsp.os.utime(flog, (self.wall, self.wall))
            watch.line('[12:00:40] copying resources into DmGame01007EF00011E000.framework')
            self.prog.end(fw_dir.name)

            steps = [e['step'] for e in self.events('step')]
            self.assertEqual(
                steps,
                [
                    'generating sources',
                    'pipeline archive (metal-tt)',
                    'generating Xcode project',
                    'xcodebuild',
                    'copying resources',
                ],
            )
            pso_prog = [e for e in self.events('progress') if e['step'] == 'pipeline archive (metal-tt)' and e['total'] == 5]
            self.assertEqual([e['done'] for e in pso_prog], [0, 1, 2, 3, 4, 5, 5])
            self.assertEqual(pso_prog[-1]['note'], 'merging')
            self.assertEqual(pso_prog[-1]['unit'], 'batches')
            self.assertEqual(pso_prog[-1]['frac'], 0.99)
            self.assertIsNotNone(pso_prog[-1]['eta'])
            xc_prog = [e for e in self.events('progress') if e['step'] == 'xcodebuild']
            self.assertEqual((xc_prog[0]['done'], xc_prog[0]['total'], xc_prog[0]['unit'], xc_prog[0]['est']), (0, 12, 'files', True))
            self.assertEqual((xc_prog[-1]['done'], xc_prog[-1]['note']), (3, 'compiling'))
            end = self.events('stage_end')[-1]
            self.assertEqual((end['name'], end['ok'], end['summary']), ('game framework', True, 'DmGame01007EF00011E000.framework'))

    def test_main_shell_framework_wires_watch_to_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            dump_dir = root / 'dump'
            (dump_dir / 'exefs').mkdir(parents=True)
            (dump_dir / 'exefs/main').write_bytes(b'main')
            (dump_dir / 'romfs').mkdir()
            shell_out = root / 'shell_fw'
            shell_out.mkdir()
            tid = 0x01007EF00011E000
            gid = f'{tid:016X}'
            fake_g = mock.MagicMock()
            fake_g.id = gid
            fake_g.title_id = tid
            fake_g.dir = root / 'profile'
            fake_g.builds.return_value = {'v0': {'display': '1.0.0', 'main': 'aa' * 16}}
            fake_g.words.side_effect = lambda k: ['v0'] if k == 'shader_versions' else ['main']
            fake_g.get.side_effect = lambda k, d='': {'name': 'Test Game', 'ipa': 'Test.ipa', 'pso_mtl4': 'on'}.get(k, d)
            fake_g.modules_in.return_value = ['main']
            fake_g.table.side_effect = lambda n: root / 'profile' / n
            (root / 'profile').mkdir()
            (root / 'profile/shader_map.tsv').write_text('')

            def fake_extract_dump(ex, ver, src, app):
                vdir = ex / ver
                (vdir / 'ExeFS').mkdir(parents=True, exist_ok=True)
                (vdir / 'ExeFS/main').write_bytes(b'main')
                (vdir / 'RomFS').mkdir(exist_ok=True)
                return vdir

            def fake_run(cmd, log, env=None, cwd=None, on_line=None, ok=(0,), fatal=True):
                if len(cmd) >= 2 and str(cmd[1]).endswith('dm/ios/tools/build_ipa.sh'):
                    self.assertIsNotNone(on_line)
                    assert on_line is not None
                    on_line('[12:00:00] generate Gen/ (01007EF00011E000, modules: main, graphics: nvn)')
                    on_line('[12:00:01] pipeline archive for g18p (metal-tt)')
                    on_line('[12:00:02] pso_offline: applegpu_g18p: batch 1/2: 1000 bases, compiling or checking cache')
                    self.now += 1.0
                    on_line('[12:00:05] pso_offline: applegpu_g18p: batch 1/2: ready (compiled)')
                    self.now += 1.0
                    on_line('[12:00:08] pso_offline: applegpu_g18p: batch 2/2: ready (compiled)')
                    on_line('[12:00:09] pso_offline: applegpu_g18p: merging 2 batches')
                    on_line('[12:00:10] framework: Gen/framework (modules main, profile.tsv, game.manifest.json)')
                    on_line(f'[12:00:11] xcodebuild (Release, unsigned, the game framework DmGame{gid} with its own host)')
                    on_line(f'[12:00:20] copying resources into DmGame{gid}.framework')
                    fw = shell_out / f'DmGame{gid}.framework'
                    fw.mkdir(parents=True, exist_ok=True)
                    (fw / 'game.manifest.json').write_text('{"schema": 1}\n')
                return 0

            fake_pg = mock.MagicMock()
            fake_pg.build_sources.return_value = {
                'graphics': ('nvn', ''),
                'glslc_capture': (False, ''),
                'vk_capture': (False, ''),
                'gl_capture': (False, ''),
                'ue_decode': (False, ''),
                'oodle_packs': (False, ''),
                'ue_legacy': (False, ''),
            }
            fake_gamemod = mock.MagicMock()
            fake_gamemod.by_title.return_value = fake_g

            patches = (
                mock.patch('builtins.input', side_effect=AssertionError('unexpected input prompt')),
                mock.patch.dict(sys.modules, {'game': fake_gamemod, 'profile_guess': fake_pg}),
                mock.patch.object(nsp, 'check_native_python'),
                mock.patch.object(nsp, 'dump_folder', return_value=True),
                mock.patch.object(nsp, 'scan_dump', return_value=(tid, {'base': dump_dir, 'updates': [], 'dlc': []})),
                mock.patch.object(nsp, 'check_prereqs'),
                mock.patch.object(nsp, 'free_gb', return_value=100.0),
                mock.patch.object(nsp, 'extract_dump', side_effect=fake_extract_dump),
                mock.patch.object(nsp, 'check_formats'),
                mock.patch.object(nsp, 'check_versions_aarch64'),
                mock.patch.object(nsp, 'build_mismatch', return_value=[]),
                mock.patch.object(nsp, 'check_metal'),
                mock.patch.object(nsp, 'extract_aoc', return_value=None),
                mock.patch.object(nsp, 'rewrite', return_value=fake_g),
                mock.patch.object(nsp, 'captured_nros', return_value=None),
                mock.patch.object(nsp, 'chain_header'),
                mock.patch.object(nsp, 'split_shaders', return_value='PK'),
                mock.patch.object(nsp, 'build_shaders', return_value=root / 'ios_sh'),
                mock.patch.object(nsp, 'uses_jit', return_value=False),
                mock.patch.object(nsp, 'report_profile_status'),
                mock.patch.object(nsp, '_persist_build_seed'),
                mock.patch.object(nsp, 'run', side_effect=fake_run),
            )
            with contextlib.ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                nsp.main(nsp.parse_args([str(dump_dir), '--shell-framework', str(shell_out), '--work-dir', str(root / 'work'), '--progress', 'json']))

            stages = [e for e in self.events('stage') if e['name'] == 'game framework']
            self.assertEqual(len(stages), 1)
            self.assertEqual(stages[0]['step'], 'starting')
            steps = [e['step'] for e in self.events('step') if e['name'] == 'game framework']
            self.assertEqual(
                steps,
                [
                    'generating sources',
                    'pipeline archive (metal-tt)',
                    'generating Xcode project',
                    'xcodebuild',
                    'copying resources',
                ],
            )
            pso_counts = [(e['done'], e['total'], e['note']) for e in self.events('progress') if e['name'] == 'game framework' and e['step'] == 'pipeline archive (metal-tt)' and e['total'] == 2]
            self.assertEqual(pso_counts, [(0, 2, ''), (1, 2, ''), (2, 2, ''), (2, 2, 'merging')])

    def test_build_ipa_and_pso_sources_emit_watched_markers(self):
        repo = SCRIPT.parents[1]
        ipa_sh = (repo / 'dm/ios/tools/build_ipa.sh').read_text(encoding='utf-8')
        pso_py = (repo / 'dm/gfx/tools/pso_offline.py').read_text(encoding='utf-8')
        for marker in (
            't "generate Gen/',
            't "pipeline archive for $GPUS',
            't "framework: Gen/framework ',
            't "xcodebuild (Release, unsigned, the game framework $FW_NAME with its own host)"',
            '> "$O/build-framework.log"',
            't "copying resources into $FW_NAME.framework"',
        ):
            self.assertIn(marker, ipa_sh)
        for marker in (
            'pso_offline: {arch}: metal-tt, {len(bases)} bases, {len(computes)} compute pipelines',
            'pso_offline: {arch}: batch {index}/{total}:',
            'pso_offline: {arch}: batch {index}/{total}: ready (',
            'pso_offline: {arch}: merging {total} batches',
            'metal-pack.{arch}.log',
        ):
            self.assertIn(marker, pso_py)


if __name__ == '__main__':
    unittest.main()
