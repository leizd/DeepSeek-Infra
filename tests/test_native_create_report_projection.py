import importlib.util,json,sys
from pathlib import Path
import pytest
SCRIPTS=Path(__file__).parents[1]/'scripts';sys.path.insert(0,str(SCRIPTS))
SPEC=importlib.util.spec_from_file_location('creation_reports',SCRIPTS/'relay_native_create_reports.py')
assert SPEC and SPEC.loader
creation=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(creation)

def test_go_event_projection_retains_skips_duplicates_failures_and_packages_without_output():
    rows=[dict(Action='run',Test='TestOne'),dict(Action='run',Test='TestOne'),dict(Action='pass',Test='TestOne'),
        dict(Action='skip',Test='TestSkipped'),dict(Action='fail',Test='TestFailed'),dict(Action='pass',Package='package'),
        dict(Action='output',Test='TestOne',Output='private diagnostic'),dict(Action='pass',Test='TestOne/subcase')]
    result=creation.go_events(('\n'.join(json.dumps(v) for v in rows)+'\nnot-json').encode())
    assert result['topLevelRuns']=={'TestOne':2}
    assert result['topLevelTerminal']=={'TestOne':'pass','TestSkipped':'skip','TestFailed':'fail'}
    assert result['packages']=={'package':'pass'} and result['nonJSONLines']==1
    assert 'private diagnostic' not in json.dumps(result)

def test_creation_report_job_has_a_cloud_guard_before_network(monkeypatch):
    monkeypatch.delenv('GITHUB_ACTIONS',raising=False)
    with pytest.raises(AssertionError):creation.main()
