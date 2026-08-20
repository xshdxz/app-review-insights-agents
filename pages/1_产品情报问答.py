"""产品情报问答：对已建语料的 App 做 RAG 深聊与跨 App 对比。"""

from __future__ import annotations

import streamlit as st

from app_review_insights.config import load_settings
from app_review_insights.factory import build_agent_stack


def _init_state(stack) -> None:
    if "rag_messages" not in st.session_state:
        st.session_state["rag_messages"] = []
    if "rag_app_ids" not in st.session_state:
        st.session_state["rag_app_ids"] = stack.agent_repository.app_ids()


def main() -> None:
    st.set_page_config(
        page_title="产品情报问答",
        page_icon=":material/forum:",
        layout="wide",
    )
    st.title("产品情报问答", anchor=False)
    st.caption("基于评论语料的证据检索问答；引用可点击查看原文。")

    settings = load_settings()
    stack = build_agent_stack(settings)
    _init_state(stack)

    app_ids = stack.agent_repository.app_ids()
    if not app_ids:
        st.info("语料库为空：请先在“证据审阅工作台”完成一次分析，或在“监控任务”页对 App 建立语料。")
        return

    mode = st.radio("问答模式", ["单 App 深聊", "跨 App 对比"], horizontal=True, key="rag-mode")
    if mode == "单 App 深聊":
        selected = st.selectbox("选择 App", app_ids, key="rag-app")
        target_ids = [selected]
        st.caption(f"检索范围：{selected} 的评论语料（不含社交舆情）")
    else:
        selected = st.multiselect("选择对比 App（2 个以上）", app_ids, key="rag-apps")
        target_ids = selected
        if len(target_ids) < 2:
            st.caption("请至少选择 2 个 App 进行对比。")

    for message in st.session_state["rag_messages"]:
        with st.chat_message(message["role"]):
            st.write(message["content"])
            for citation in message.get("citations", []):
                with st.expander(f"证据引用 · {citation['review_id']}"):
                    st.write(citation["quote"])

    question = st.chat_input("例如：用户最不满的是什么？订阅转化差的原因？")
    if question and target_ids:
        st.session_state["rag_messages"].append(
            {"role": "user", "content": question, "citations": []}
        )
        with st.chat_message("user"):
            st.write(question)
        with st.spinner("检索语料并生成回答…"):
            answer = stack.rag.answer(question, target_ids)
        message = {
            "role": "assistant",
            "content": answer.answer,
            "citations": [c.model_dump() for c in answer.citations],
        }
        st.session_state["rag_messages"].append(message)
        with st.chat_message("assistant"):
            st.write(answer.answer)
            if answer.citations:
                for citation in answer.citations:
                    with st.expander(f"证据引用 · {citation.review_id}"):
                        st.write(citation.quote)
            else:
                st.caption(answer.limitation)


main()
