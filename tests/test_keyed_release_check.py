import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import yaml

from tools.keyed_release_check import verify
from tools.keyed_vps_check import NEW

ROOT=Path(__file__).resolve().parents[1]


class ReleaseCheckTests(unittest.TestCase):
    def stage(self, root):
        cfg=yaml.safe_load((ROOT/'config/config.yaml').read_text(encoding='utf8'))
        (root/'config').mkdir();(root/'config/config.yaml').write_text(yaml.safe_dump(cfg),encoding='utf8')
        (root/'evidence/keyed_20261101').mkdir(parents=True)
        (root/'evidence/keyed_20261101/LIBERACAO_PREPARO.json').write_text(
            json.dumps({'gate_passed':True,'economic_source_sha256':{}}),encoding='utf8')
        manifest={'files_sha256':{str(p.relative_to(root)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in root.rglob('*') if p.is_file()}}
        (root/'RELEASE_MANIFEST.json').write_text(json.dumps(manifest),encoding='utf8')
        return cfg

    def test_preflight_accepts_prepared_configuration_and_refuses_unapproved_live_drift(self):
        with TemporaryDirectory(dir=ROOT/'data/test_tmp') as tmp:
            root=Path(tmp);cfg=self.stage(root)
            self.assertTrue(verify(root)['passed'])
            for key in (*NEW,'keyed_policy','structure_repair','keyed_validation'):
                cfg['instrumentation'].pop(key)
            live=root/'live.yaml';live.write_text(yaml.safe_dump(cfg),encoding='utf8')
            self.assertTrue(verify(root,live)['passed'])
            cfg['capital']['trade_size_pct']+=1;live.write_text(yaml.safe_dump(cfg),encoding='utf8')
            with self.assertRaisesRegex(ValueError,'Unapproved live config'):
                verify(root,live)

    def test_manifest_tampering_or_fallback_policy_drift_is_rejected(self):
        with TemporaryDirectory(dir=ROOT/'data/test_tmp') as tmp:
            root=Path(tmp);cfg=self.stage(root)
            for key in ('dmi15_trajectory_context_shadow','hs_bull_elastic_shadow'):
                cfg['instrumentation'][key]['enabled']=False
            candidate=root/'fallback.yaml';candidate.write_text(yaml.safe_dump(cfg),encoding='utf8')
            self.assertTrue(verify(root,candidate_config=candidate,fallback=True)['passed'])
            cfg['capital']['trade_size_pct']+=1;candidate.write_text(yaml.safe_dump(cfg),encoding='utf8')
            with self.assertRaisesRegex(ValueError,'beyond authorized fallback'):
                verify(root,candidate_config=candidate,fallback=True)
            with (root/'config/config.yaml').open('a') as f:f.write('\n# drift\n')
            with self.assertRaisesRegex(ValueError,'hash mismatch'):
                verify(root)


if __name__=='__main__':unittest.main()
