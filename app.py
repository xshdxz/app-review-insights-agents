import streamlit as st


def main() -> None:
    st.set_page_config(page_title="App Review Insights", page_icon="📱", layout="wide")
    st.title("App Review Insights")
    st.caption("从美国区 App Store 评论到产品发现、PRD 与可追溯测试用例")
    st.info("项目基础环境已经就绪，完整分析工作台将在后续开发阶段接入。")


if __name__ == "__main__":
    main()
