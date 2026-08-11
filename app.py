"""
Docling(오픈소스 문서 파싱 AI) vs 현재 pdftotext 방식 비교용 테스트 앱.

이 앱 하나만 따로 배포해서 "우리 서버 환경(Streamlit Cloud)에서 Docling이
실제로 돌아가는지, MSDS 성분표를 얼마나 정확히 읽는지"를 확인하기 위한
용도다. 메인 MSDS 앱과는 완전히 분리되어 있어 여기서 뭐가 잘못돼도
메인 서비스에는 영향이 없다.

확인하려는 것 두 가지:
1) Docling이 이 배포 환경(메모리 제한 등)에서 정상적으로 동작하는가
2) 동작한다면, 우리 MSDS 표(제조사/성분명/CAS/함유량)를 pdftotext보다
   더 정확하게 읽어내는가
"""
import time

import streamlit as st

import msds_core as core

st.set_page_config(page_title="Docling 테스트", page_icon="🧪", layout="wide")

st.title("🧪 Docling vs 현재 방식 비교 테스트")
st.caption(
    "이 화면은 실험용입니다. MSDS PDF를 올리면 현재 방식(pdftotext)과 "
    "Docling(오픈소스 AI) 두 가지로 각각 읽어서 나란히 보여줍니다."
)

uploaded = st.file_uploader("MSDS PDF 업로드", type=["pdf"])

if uploaded:
    # 업로드된 파일을 임시 경로에 저장 (두 방식 모두 파일 경로가 필요함)
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(uploaded.getvalue())
        tmp_path = tmp.name

    col1, col2 = st.columns(2)

    # ── 1) 현재 방식 ──────────────────────────────────────────────
    with col1:
        st.subheader("① 현재 방식 (pdftotext)")
        t = time.time()
        text_a, method = core.extract_text(tmp_path)
        ms_a = (time.time() - t) * 1000
        res_a = core.parse_msds(text_a, uploaded.name)
        st.caption(f"방식: {method}  ·  소요 시간: {ms_a:.0f} ms")

        st.write("**제조사:**", res_a.get("manufacturer") or "(없음)")
        st.write("**사용용도:**", res_a.get("use") or "(없음)")

        comps_a = res_a.get("comps", [])
        st.write(f"**성분표 ({len(comps_a)}개):**")
        st.dataframe(
            [{"성분명": c.get("name"), "CAS": c.get("cas"), "함유량": c.get("pct")} for c in comps_a],
            width="stretch",
            hide_index=True,
        )

    # ── 2) Docling ────────────────────────────────────────────────
    docling_ok = False
    comps_b = []
    with col2:
        st.subheader("② Docling (오픈소스 AI)")
        try:
            with st.spinner("Docling 모델 로딩 중... (처음 한 번은 몇 분 걸릴 수 있어요)"):
                from docling.document_converter import DocumentConverter
                # st.cache_resource로 감싸서 이후 업로드부터는 모델을 재사용한다
                @st.cache_resource(show_spinner=False)
                def _get_converter():
                    return DocumentConverter()
                converter = _get_converter()

            t = time.time()
            doc = converter.convert(tmp_path).document
            text_b = doc.export_to_markdown()
            ms_b = (time.time() - t) * 1000
            res_b = core.parse_msds(text_b, uploaded.name)
            docling_ok = True

            st.caption(f"소요 시간: {ms_b:.0f} ms")
            st.write("**제조사:**", res_b.get("manufacturer") or "(없음)")
            st.write("**사용용도:**", res_b.get("use") or "(없음)")

            comps_b = res_b.get("comps", [])
            st.write(f"**성분표 ({len(comps_b)}개):**")
            st.dataframe(
                [{"성분명": c.get("name"), "CAS": c.get("cas"), "함유량": c.get("pct")} for c in comps_b],
                width="stretch",
                hide_index=True,
            )

            with st.expander("Docling 원본 Markdown 출력 보기"):
                st.text(text_b[:5000])

        except Exception as e:
            st.error(f"Docling 실행 실패: {type(e).__name__}: {e}")
            st.info(
                "메모리 부족(OOM)이거나 모델 다운로드가 막혔을 가능성이 높습니다. "
                "이 환경에서는 Docling을 쓰기 어렵다는 뜻으로 봐도 됩니다."
            )

    # ── 요약 채움률 비교 ──────────────────────────────────────────
    st.divider()
    filled_a = sum(1 for c in comps_a if c.get("pct"))
    summary = f"**함유량 채움률** — 현재 방식: {filled_a}/{len(comps_a)}"
    if docling_ok:
        filled_b = sum(1 for c in comps_b if c.get("pct"))
        summary += f"  ·  Docling: {filled_b}/{len(comps_b)}"
    st.write(summary)
