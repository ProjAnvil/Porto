from __future__ import annotations

import pytest

from porto_chatbot.settings import Settings


@pytest.fixture
def store(tmp_path):
    from porto_chatbot.memory.facts import SessionFactsStore
    from porto_chatbot.memory.session_store import SessionStore

    settings = Settings(data_dir=tmp_path)
    SessionStore(settings)  # 触发 _init_db
    return SessionFactsStore(settings)


def test_upsert_new_fact(store):
    fid = store.upsert(
        session_id="s1", category="user_decision",
        content="登录采用 OAuth", source_msg_id="m1",
    )
    assert fid
    active = store.list_active("s1")
    assert len(active) == 1
    assert active[0].content == "登录采用 OAuth"
    assert active[0].status == "active"


def test_upsert_updates_when_similar(store):
    store.upsert(session_id="s1", category="user_decision",
                 content="登录采用 OAuth", source_msg_id="m1")
    store.upsert(session_id="s1", category="user_decision",
                 content="登录采用 OAuth 方式", source_msg_id="m2")  # Jaccard ≥ 0.5
    active = store.list_active("s1")
    assert len(active) == 1  # 不是新增,是更新
    assert "OAuth 方式" in active[0].content


def test_upsert_adds_when_dissimilar(store):
    store.upsert(session_id="s1", category="user_decision",
                 content="登录采用 OAuth", source_msg_id="m1")
    store.upsert(session_id="s1", category="user_decision",
                 content="前端用 React 做表格组件", source_msg_id="m2")  # 无重叠
    active = store.list_active("s1")
    assert len(active) == 2


def test_upsert_evicts_oldest_when_category_full(store):
    # 注意:4 条内容必须互相 Jaccard < threshold,否则会走 update 分支
    # 而非 insert+evict,导致测试根本没演练到淘汰路径。
    # 旧版用 f"偏好编号 {i} 各不相同",共享 CJK bigram,Jaccard≈0.89,无效。
    contents_in_order = [
        "前端框架选 React",
        "数据库选 Postgres",
        "消息队列选 Kafka",
        "部署平台选 Vercel",
    ]
    store.settings.facts_max_per_category = 3
    for i, content in enumerate(contents_in_order):
        store.upsert(session_id="s1", category="user_preference",
                     content=content, source_msg_id=f"m{i}")
    active = store.list_active("s1")
    assert len(active) == 3
    contents = [f.content for f in active]
    assert "前端框架选 React" not in contents  # 最旧的被淘汰
    assert "部署平台选 Vercel" in contents     # 最新保留


def test_upsert_scoped_by_session_category(store):
    store.upsert(session_id="s1", category="user_decision",
                 content="登录采用 OAuth", source_msg_id="m1")
    store.upsert(session_id="s2", category="user_decision",
                 content="登录采用 OAuth", source_msg_id="m1")  # 同内容不同 session
    assert len(store.list_active("s1")) == 1
    assert len(store.list_active("s2")) == 1


def test_retract_marks_status(store):
    fid = store.upsert(session_id="s1", category="user_decision",
                       content="登录采用 OAuth", source_msg_id="m1")
    store.retract(fid)
    active = store.list_active("s1")
    assert len(active) == 0  # retracted 的不进 active


def test_retract_idempotent(store):
    fid = store.upsert(session_id="s1", category="user_decision",
                       content="登录采用 OAuth", source_msg_id="m1")
    store.retract(fid)
    store.retract(fid)  # 不报错


def test_list_active_orders_by_category_priority(store):
    store.upsert(session_id="s1", category="open_question",
                 content="前端框架未定", source_msg_id="m1")
    store.upsert(session_id="s1", category="user_decision",
                 content="登录采用 OAuth", source_msg_id="m2")
    store.upsert(session_id="s1", category="user_preference",
                 content="后端用 Go", source_msg_id="m3")
    active = store.list_active("s1")
    assert [f.category for f in active] == [
        "user_decision", "user_preference", "open_question",
    ]


def test_by_category_groups(store):
    store.upsert(session_id="s1", category="user_decision",
                 content="登录采用 OAuth", source_msg_id="m1")
    store.upsert(session_id="s1", category="user_decision",
                 content="不用 SAML", source_msg_id="m2")
    store.upsert(session_id="s1", category="project_context",
                 content="金融客户项目", source_msg_id="m3")
    grouped = store.by_category("s1")
    assert len(grouped["user_decision"]) == 2
    assert len(grouped["project_context"]) == 1
    assert "open_question" not in grouped  # 空组不出现
