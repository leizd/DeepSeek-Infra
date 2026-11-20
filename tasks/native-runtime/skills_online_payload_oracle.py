"""Offline request-composition oracle. The injected callable never opens a network connection."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))

from deepseek_infra.infra.skills.runner import _llm_output  # noqa: E402
from deepseek_infra.infra.skills.schema import validate_skill_config  # noqa: E402


def fixtures() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]]=[]
    for name,tools,memory,model,browser in [
        ('no-tools',[],{'read':False,'scope':'none'},'',{}),
        ('web-search',['web_search'],{'read':False,'scope':'none'},'deepseek-v4-pro',{}),
        ('compare-search',['compare_search_results'],{'read':False,'scope':'none'},'deepseek-v4-pro',{}),
        ('both-search',['web_search','compare_search_results'],{'read':False,'scope':'none'},'',{}),
        ('memory-global',[],{'read':True,'scope':'global'},'',{}),
        ('filtered-browser',['browser_click','browser_type_text','web_search'],{'read':False,'scope':'none'},'',{'allowClick':False,'allowType':True}),
    ]:
        skill=validate_skill_config({'skillId':'online-payload','name':'在线 Skill 参数',
            'description':'离线原函数参数对照','version':'1.0','systemPrompt':'保留输入并说明依据。',
            'inputSchema':{'type':'object'},'outputSchema':{'type':'object','required':['content']},
            'allowedTools':tools,'memoryPolicy':memory,'browserPolicy':browser,
            'artifactPolicy':{'types':[],'autoSave':False},'projectBinding':{'enabled':False}})
        input_data={'topic':'中文输入','details':{'values':[1,True,None],'note':'保留内容'}}
        captured: list[dict[str, Any]]=[]

        def capture(payload: dict[str, Any], *, parent_span_id: str='') -> dict[str, Any]:
            captured.append(payload)
            return {'content':'offline payload capture only'}

        _llm_output(skill,input_data,api_key='fixture-online-key',tavily_api_key='fixture-search-key',
            model=model,llm_callable=capture,parent_span_id='offline-oracle-parent')
        assert len(captured)==1
        options={'apiKey':'fixture-online-key','tavilyApiKey':'fixture-search-key','model':model,
            'persist':False,'securityApproved':True}
        cases.append({'name':name,'skill':skill,'input':input_data,'options':options,'expectedRequest':captured[0]})
    return cases


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    value={'scope':'offline Python _llm_output request composition only; no network or production execution','cases':fixtures()}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_bytes((json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode('utf8'))
    print(json.dumps({'cases':len(value['cases']),'output':args.output.as_posix(),'networkCalls':0}))
