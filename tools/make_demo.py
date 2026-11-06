"""生成演示资料，用于验证 EveryFind 的解析与问答能力。

用法：python tools/make_demo.py [输出目录]
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

if os.name == "nt":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "演示资料"
OUT.mkdir(parents=True, exist_ok=True)


# ─────────────────────────── Word ───────────────────────────
def make_docx() -> None:
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    doc.core_properties.title = "云舟项目交付要求"
    doc.core_properties.author = "项目管理办公室"

    def h(text, level):
        doc.add_heading(text, level=level)

    def p(text):
        doc.add_paragraph(text)

    h("云舟项目交付要求", 0)
    p("文档编号：YQ-2026-DEL-001　　版本：V1.2　　密级：内部")

    h("一、项目概述", 1)
    p("云舟平台交付建设项目的最终交付物包括验收报告、操作手册与问题清单三类文档。"
      "本文件用于明确三类交付物的编制口径、责任人以及提交时间要求。"
      "各责任人须在对应节点前完成编制与内部复核，由项目经理统一汇总提交。")

    h("二、交付物清单", 1)
    p("交付物清单：验收报告、操作手册、问题清单。")
    p("除上述文档外，交付时还需附带源文件与测试用例说明，作为归档材料一并提交。")

    h("三、责任分工", 1)
    p("验收报告由项目经理负责汇总和提交。")
    p("操作手册由技术负责人负责编写和提交。")
    p("问题清单由测试负责人负责整理和提交。")
    p("源文件与测试用例说明由技术负责人随操作手册一并提交。")

    h("四、验收标准", 1)
    p("验收报告须覆盖全部功能点与性能指标，遗留问题数量不超过 3 个。")
    p("操作手册须包含安装部署、配置说明与常见问题处理三个章节。")
    p("问题清单须逐条标注严重级别、复现步骤与责任人。")

    h("五、时间安排", 1)
    p("第一阶段：资料整理，第1周。")
    p("第二阶段：方案实施，第2至3周。")
    p("第三阶段：验收交付，第4周。")

    h("六、签署", 1)
    p("项目经理：张岚　　技术负责人：陈亦然　　测试负责人：林知予")

    doc.save(str(OUT / "云舟项目交付要求.docx"))
    print("[ok] 云舟项目交付要求.docx")


# ─────────────────────────── Excel ───────────────────────────
def make_xlsx() -> None:
    import openpyxl
    from openpyxl.styles import Alignment, Font

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "2026预算"

    ws["A1"] = "年度预算表"
    ws["A1"].font = Font(size=16, bold=True)
    ws.merge_cells("A1:D1")
    ws["A2"] = "编制单位：云舟项目组"
    ws["B2"] = "编制日期：2026-01-10"

    ws.append(["项目", "预算（元）", "实际支出（元）", "差异"])
    for c in ws[3]:
        c.font = Font(bold=True)
        c.alignment = Alignment(horizontal="center")

    rows = [
        ("差旅费", 48000, 32000),
        ("设备采购", 120000, 118000),
        ("外包服务", 60000, 55000),
        ("培训费", 20000, 15000),
        ("合计", 248000, 220000),
    ]
    for name, budget, actual in rows:
        ws.append([name, budget, actual, budget - actual])

    for col, w in zip("ABCD", (22, 16, 18, 14)):
        ws.column_dimensions[col].width = w

    ws2 = wb.create_sheet("说明")
    ws2["A1"] = "预算说明"
    ws2["A2"] = "1. 差旅费预算含 6 次出差，含住宿与市内交通。"
    ws2["A3"] = "2. 设备采购含 3 台测试服务器与 2 台工作站。"
    ws2["A4"] = "3. 实际支出以 2025 年 12 月 31 日财务结算为准。"
    ws2.column_dimensions["A"].width = 60

    wb.save(str(OUT / "年度预算.xlsx"))
    print("[ok] 年度预算.xlsx")


# ─────────────────────────── PPT ───────────────────────────
def make_pptx() -> None:
    from pptx import Presentation
    from pptx.util import Inches, Pt

    prs = Presentation()

    def slide(title, bullets, title_only=False):
        s = prs.slides.add_slide(prs.slide_layouts[1] if not title_only else prs.slide_layouts[0])
        if title_only:
            s.shapes.title.text = title
            return s
        s.shapes.title.text = title
        tf = s.placeholders[1].text_frame
        tf.text = bullets[0]
        for b in bullets[1:]:
            p = tf.add_paragraph()
            p.text = b
            p.level = 0
        for para in tf.paragraphs:
            for r in para.runs:
                r.font.size = Pt(18)
        return s

    s = prs.slides.add_slide(prs.slide_layouts[0])
    s.shapes.title.text = "云舟项目方案"
    s.placeholders[1].text = "汇报人：张岚　　2026 年 1 月"

    slide("项目背景", [
        "云舟平台进入交付周期，需要在 4 周内完成资料整理、方案实施与验收交付。",
        "核心诉求：交付物齐全、责任到人、验收标准可核对。",
    ])
    slide("执行计划", [
        "第一阶段：资料整理，第1周。",
        "第二阶段：方案实施，第2至3周。",
        "第三阶段：验收交付，第4周。",
    ])
    slide("交付物与责任人", [
        "验收报告由项目经理负责汇总和提交。",
        "操作手册由技术负责人负责编写和提交。",
        "问题清单由测试负责人负责整理和提交。",
    ])
    slide("预算概览", [
        "年度总预算 248,000 元，实际支出 220,000 元。",
        "其中差旅费预算 48,000 元，实际支出 32,000 元。",
    ])
    slide("风险与对策", [
        "风险一：验收遗留问题超 3 个。对策：第 2 周末做一次预验收。",
        "风险二：测试人力不足。对策：外包 2 名测试工程师支援。",
    ])

    prs.save(str(OUT / "云舟项目方案.pptx"))
    print("[ok] 云舟项目方案.pptx")


# ─────────────────────────── 图片（OCR） ───────────────────────────
def make_image() -> None:
    from PIL import Image, ImageDraw, ImageFont

    w, h = 1240, 1754
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)

    def font(size):
        for path in ("C:/Windows/Fonts/simsun.ttc", "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf"):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
        return ImageFont.load_default()

    lines_title = ["国务院办公厅关于2025年", "部分节假日安排的通知"]
    y = 110
    for t in lines_title:
        d.text((w // 2, y), t, fill="black", font=font(46), anchor="ma")
        y += 68
    d.text((w // 2, y + 10), "国办发明电〔2024〕12号", fill="black", font=font(30), anchor="ma")

    y = 320
    body = [
        "各省、自治区、直辖市人民政府，国务院各部委、各直属机构：",
        "经党中央、国务院批准，现将2025年元旦、春节、清明节、劳动节、端午节、中秋节和国庆节放假调休日期的具体安排通知如下。",
        "一、元旦：1月1日（周三）放假1天。",
        "二、春节：1月28日（农历除夕、周二）至2月4日（农历正月初七、周二）放假调休，共8天。",
        "2月8日（周六）、2月9日（周日）上班。",
        "三、清明节：4月4日（周五）至6日（周日）放假调休，共3天。",
        "四、劳动节：5月1日（周四）至5日（周一）放假调休，共5天。",
        "5月2日（周六）至4日（周一）上班。",
        "五、端午节：5月31日（周六）至6月2日（周一）放假，共3天。",
        "六、国庆节、中秋节：10月1日（周三）至8日（周日）放假调休，共8天。",
        "9月28日（周日）、10月11日（周六）上班。",
        "节假日期间，各地区各部门要认真做好节假日值守工作，加强安全防范和应急管理，确保人民群众平安祥和度过节日假期。",
    ]

    def wrap(text, width=33):
        """按宽度折行，并遵守中文避头尾规则（标点不作为下一行开头）。"""
        lines = []
        i = 0
        while i < len(text):
            end = min(i + width, len(text))
            if end < len(text):
                for j in range(end, max(i + width - 6, i), -1):
                    if text[j] in "、，。：；）】》！？":
                        end = j + 1
                        break
            lines.append(text[i:end])
            i = end
        return lines or [""]

    for t in body:
        segs = wrap(t)
        for k, seg in enumerate(segs):
            x = 120 if k == 0 else 168
            d.text((x, y), seg, fill="black", font=font(30))
            y += 52
        y += 14

    d.text((w - 140, h - 170), "国务院办公厅", fill="black", font=font(32), anchor="ra")
    d.text((w - 140, h - 120), "2024年11月12日", fill="black", font=font(32), anchor="ra")

    img.save(str(OUT / "国务院办公厅关于2025年部分节假日安排的通知.jpg"), quality=92)
    print("[ok] 国务院办公厅关于2025年部分节假日安排的通知.jpg")


# ─────────────────────────── PDF ───────────────────────────
HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><style>
@page { size: A4; margin: 22mm 20mm; }
body { font-family: "SimSun", serif; font-size: 12pt; line-height: 1.9; color: #111; }
h1 { text-align: center; font-size: 18pt; margin: 0 0 6pt; }
h2 { text-align: center; font-size: 14pt; margin: 0 0 4pt; font-weight: normal; }
.sub { text-align: center; font-size: 10.5pt; margin-bottom: 22pt; }
p { text-indent: 2em; margin: 6pt 0; text-align: justify; }
.sign { text-align: right; margin-top: 30pt; }
</style></head><body>
<h1>云舟项目验收报告</h1>
<h2>YQ-2026-DEL-001-A</h2>
<div class="sub">编制单位：云舟项目组　　编制日期：2026-01-20</div>
<p>本验收报告依据《云舟项目交付要求》编制，用于说明云舟平台第一阶段交付成果的验证情况。报告覆盖功能验证、性能验证与遗留问题统计三个部分。</p>
<p>一、功能验证。共执行测试用例 246 条，通过 243 条，通过率 98.8%。核心业务流程（资料检索、问答溯源、批量索引）全部通过验证。</p>
<p>二、性能验证。在 5000 份文档规模的测试集上，全文检索平均耗时 0.42 秒，引用溯源响应平均耗时 1.1 秒，满足验收指标要求。</p>
<p>三、遗留问题。当前未关闭问题 2 个，严重级别均为一般，不影响主流程使用。问题清单由测试负责人林知予整理并随本报告一并提交。</p>
<p>四、结论。经项目组评审，云舟平台第一阶段交付成果符合《云舟项目交付要求》第四章验收标准，同意进入第二阶段方案实施。</p>
<div class="sign">项目经理：张岚<br>2026 年 1 月 20 日</div>
</body></html>"""


def make_pdf() -> None:
    html_path = OUT / "_notice.html"
    pdf_path = OUT / "云舟项目验收报告.pdf"
    html_path.write_text(HTML, encoding="utf-8")

    edge_candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        shutil.which("msedge") or "",
    ]
    edge = next((e for e in edge_candidates if e and Path(e).exists()), None)
    if not edge:
        print("· 未找到 Edge，跳过 PDF 生成")
        html_path.unlink(missing_ok=True)
        return

    try:
        subprocess.run(
            [edge, "--headless", "--disable-gpu", "--no-pdf-header-footer",
             f"--print-to-pdf={pdf_path}", html_path.as_uri()],
            check=True, capture_output=True, timeout=90,
        )
    except Exception as exc:
        print(f"· PDF 生成失败：{exc}")
        html_path.unlink(missing_ok=True)
        return

    html_path.unlink(missing_ok=True)
    if pdf_path.exists():
        print("[ok] 云舟项目验收报告.pdf")
    else:
        print("· PDF 未生成")


if __name__ == "__main__":
    make_docx()
    make_xlsx()
    make_pptx()
    make_image()
    make_pdf()
    print(f"\n演示资料已生成于：{OUT}")
