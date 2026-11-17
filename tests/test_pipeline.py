from __future__ import annotations

import json

from askdata.pipeline import Pipeline


def test_pipeline_repairs_once(catalog, index, fake):
    bad = {'table': 'orders', 'select': [{'agg': 'sum', 'column': 'amount'}]}
    good = {'table': 'orders', 'select': [{'agg': 'sum', 'column': 'pay_amount', 'as': 'GMV'}]}
    model = fake(
        [
            '{"intent": "query", "keywords": ["订单", "金额"]}',
            '{"tables": ["orders"]}',
            json.dumps(bad),
            '```json\n' + json.dumps(good) + '\n```',
        ]
    )
    answer = Pipeline(catalog, model, index).ask('订单总金额')
    assert answer.kind == 'result' and answer.attempts == 2
    assert 'amount 不存在' in answer.errors[0]
    # 第二次生成时，模型看到了上一次的错误
    assert '有问题' in model.requests[-1][-1]['content']


def test_pipeline_reply_intent(catalog, index, fake):
    answer = Pipeline(catalog, fake(['{"intent": "reply", "reply": "你好"}']), index).ask('你好')
    assert answer.kind == 'reply' and answer.text == '你好'


def test_pipeline_no_table(catalog, index, fake):
    model = fake(['{"intent": "query", "keywords": ["天气"]}'])
    answer = Pipeline(catalog, model, index).ask('明天天气')
    assert answer.kind == 'reply' and model.usage.calls == 1
