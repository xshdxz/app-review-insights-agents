"""语料库管理：查看、搜索、删除已索引评论，统计分布。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import streamlit as st

_DB_PATH = Path("data/agent/agent.sqlite3")


def _conn():
    c = sqlite3.connect(str(_DB_PATH))
    c.row_factory = sqlite3.Row
    return c


def _render_stats():
    """语料库统计概览。"""
    conn = _conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM corpus").fetchone()[0]
        fts = conn.execute("SELECT COUNT(*) FROM corpus_fts").fetchone()[0]
        try:
            emb = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        except Exception:
            emb = 0
        apps = conn.execute(
            "SELECT app_id, platform, COUNT(*) as cnt FROM corpus "
            "GROUP BY app_id, platform ORDER BY cnt DESC"
        ).fetchall()
    finally:
        conn.close()

    cols = st.columns(3)
    cols[0].metric("总评论数", total)
    cols[1].metric("FTS 索引", fts)
    cols[2].metric("向量", emb)

    if apps:
        st.markdown("**按 App 分布：**")
        for a in apps:
            st.write(f"- `{a['app_id']}` ({a['platform']})：{a['cnt']} 条")


def _render_search():
    """关键词搜索语料库。"""
    st.subheader("搜索语料", anchor=False)
    query = st.text_input("搜索关键词", placeholder="订阅 / timer / crash", key="corpus-q")
    app_filter = st.text_input("App ID 过滤（可选）", placeholder="839285684", key="corpus-app")
    limit = st.slider("返回条数", 5, 100, 20, key="corpus-limit")

    if not query:
        return

    conn = _conn()
    try:
        app_ids = [a.strip() for a in app_filter.split(",") if a.strip()] if app_filter else None
        # 构造 FTS 查询
        fts_query = " OR ".join(query.split())
        if app_ids:
            placeholders = ",".join("?" for _ in app_ids)
            rows = conn.execute(
                "SELECT c.review_id, c.app_id, c.content, c.platform, c.region, c.source "
                "FROM corpus c JOIN corpus_fts f ON c.review_id = f.review_id "
                f"WHERE corpus_fts MATCH ? AND c.app_id IN ({placeholders}) "
                "ORDER BY rank LIMIT ?",
                [fts_query, *app_ids, limit],
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT c.review_id, c.app_id, c.content, c.platform, c.region, c.source "
                "FROM corpus c JOIN corpus_fts f ON c.review_id = f.review_id "
                "WHERE corpus_fts MATCH ? ORDER BY rank LIMIT ?",
                [fts_query, limit],
            ).fetchall()
    except Exception as e:
        st.error(f"搜索失败：{e}")
        return
    finally:
        conn.close()

    if not rows:
        st.info("未找到匹配评论")
        return

    st.write(f"找到 {len(rows)} 条：")
    for r in rows:
        with st.container(border=True):
            rid = r['review_id']
            aid = r['app_id']
            st.markdown(f"**`{rid}`** · app=`{aid}` · {r['platform']} · {r['region']}")
            st.caption(r["content"][:300])


def _render_delete():
    """删除语料（按 App 或全部）。"""
    st.subheader("管理语料", anchor=False)
    conn = _conn()
    try:
        apps = conn.execute(
            "SELECT app_id, COUNT(*) as cnt FROM corpus GROUP BY app_id ORDER BY cnt DESC"
        ).fetchall()
    finally:
        conn.close()

    if not apps:
        st.info("语料库为空")
        return

    app_options = [f"{a['app_id']} ({a['cnt']} 条)" for a in apps]
    selected = st.selectbox("选择要清除的 App", app_options, key="corpus-delete-app")

    if st.button("清除所选 App 的语料", type="primary", key="corpus-delete-btn"):
        app_id = selected.split(" ")[0]
        conn = _conn()
        try:
            # 先删 embeddings（如有）
            conn.execute(
                "DELETE FROM embeddings WHERE review_id IN "
                "(SELECT review_id FROM corpus WHERE app_id = ?)", [app_id]
            )
            # 删 FTS
            conn.execute(
                "DELETE FROM corpus_fts WHERE review_id IN "
                "(SELECT review_id FROM corpus WHERE app_id = ?)", [app_id]
            )
            # 删 corpus
            conn.execute("DELETE FROM corpus WHERE app_id = ?", [app_id])
            conn.commit()
            st.success(f"已清除 app_id={app_id} 的语料")
            st.rerun()
        except Exception as e:
            st.error(f"清除失败：{e}")
        finally:
            conn.close()

    if st.button("清空全部语料", key="corpus-delete-all"):
        conn = _conn()
        try:
            conn.execute("DELETE FROM embeddings")
            conn.execute("DELETE FROM corpus_fts")
            conn.execute("DELETE FROM corpus")
            conn.commit()
            st.success("语料库已清空")
            st.rerun()
        except Exception as e:
            st.error(f"清空失败：{e}")
        finally:
            conn.close()


def main() -> None:
    st.set_page_config(
        page_title="语料库管理",
        page_icon=":material/database:",
        layout="wide",
    )
    st.title("语料库管理", anchor=False)
    st.caption("查看已索引评论、搜索语料、管理语料库内容。")

    _render_stats()
    st.divider()
    _render_search()
    st.divider()
    _render_delete()


main()
