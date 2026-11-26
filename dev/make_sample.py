# -*- coding: utf-8 -*-
"""
生成一份模拟的双栏学术 PDF，用于验证抽取与切分逻辑。

注意：它只能验证「代码逻辑对不对」（双栏是否分开、页眉页脚是否剔除、
参考文献是否截断），验证不了「真实排版的质量」——那必须用真文献。

  python dev/make_sample.py
"""

import sys
from pathlib import Path

import pymupdf

OUT = Path(__file__).resolve().parent / "sample_two_column.pdf"

PW, PH = 595, 842          # A4
MARGIN = 42
GUTTER = 22
COL_W = (PW - 2 * MARGIN - GUTTER) / 2
BODY_FS = 9
LEAD = BODY_FS * 1.36
TOP_Y = 62
BOTTOM_Y = PH - 58

TITLE = ("Association Between Nocturnal Sleep Duration and Cardiovascular Risk "
         "in Middle-Aged Adults: A Prospective Cohort Study")

ABS_TEXT = (
    "Background: Short and long sleep durations have both been linked to adverse cardiovascular "
    "outcomes, but whether the relationship is causal or confounded by socioeconomic and "
    "behavioural factors remains uncertain. Methods: We followed 12,438 participants aged 45 to 64 "
    "years without baseline cardiovascular disease for a median of 9.4 years. Sleep duration was "
    "self-reported at baseline and categorised into five groups. Cox proportional hazards models "
    "were used to estimate hazard ratios for major adverse cardiovascular events, adjusting for age, "
    "sex, smoking, alcohol intake, body mass index, education, and depressive symptoms. Results: "
    "Compared with the reference group of seven to eight hours, participants sleeping five hours or "
    "less had a higher risk of major adverse cardiovascular events (hazard ratio 1.34, 95% confidence "
    "interval 1.18 to 1.52). Conclusions: Both short and long sleep duration were associated with "
    "elevated cardiovascular risk in a dose dependent manner, with the lowest risk observed at seven "
    "to eight hours per night."
)

SECTIONS = [
    ("1. Introduction", [
        "Cardiovascular disease remains the leading cause of death worldwide, accounting for an "
        "estimated 17.9 million deaths each year. Over the past two decades, a growing body of "
        "epidemiological evidence has implicated sleep duration as a modifiable risk factor, "
        "independent of traditional determinants such as hypertension, dyslipidaemia, and diabetes "
        "mellitus. The observation that both insufficient and excessive sleep confer risk has been "
        "reported in more than thirty prospective cohorts across diverse populations.",

        "Several mechanistic pathways have been proposed to explain this association. Experimental "
        "sleep restriction studies demonstrate activation of the sympathetic nervous system, "
        "elevation of evening cortisol, and impairment of endothelial function after only five "
        "consecutive nights of restricted sleep. In parallel, long sleep duration may serve as a "
        "marker of underlying comorbidity, inflammation, or subclinical disease rather than a causal "
        "exposure in its own right.",

        "Previous meta analyses have reported pooled relative risks in the range of 1.2 to 1.5 for "
        "short sleep, but substantial heterogeneity exists across studies. Differences in sleep "
        "assessment methods, follow up duration, and adjustment sets complicate direct comparison. "
        "In particular, few studies have simultaneously accounted for depressive symptoms and "
        "socioeconomic position, both of which are strongly associated with both sleep duration and "
        "cardiovascular outcomes.",
    ]),
    ("2. Methods", [
        "The present analysis used data from the Regional Heart and Vascular Cohort, a prospective "
        "population based study initiated in 2008. Eligible participants were community dwelling "
        "adults aged 45 to 64 years who completed a baseline examination including a structured "
        "interview, physical measurements, and fasting blood sampling. We excluded individuals with "
        "a history of myocardial infarction, stroke, heart failure, or atrial fibrillation at "
        "baseline, as well as those with missing sleep data.",

        "Sleep duration was ascertained using a single item: On average, how many hours of sleep do "
        "you get in a 24 hour period. Responses were recorded to the nearest half hour and "
        "categorised as five hours or less, six hours, seven to eight hours, nine hours, and ten "
        "hours or more. The seven to eight hour category served as the reference throughout.",

        "The primary outcome was a composite of cardiovascular death, nonfatal myocardial "
        "infarction, and nonfatal stroke, ascertained through linkage with national hospital "
        "discharge and death registries. Participants were followed from the baseline examination "
        "until the first event, death from any cause, or the end of follow up in December 2023, "
        "whichever occurred first. Cox proportional hazards regression was used to estimate hazard "
        "ratios with 95 percent confidence intervals. The proportional hazards assumption was "
        "verified using scaled Schoenfeld residuals.",
    ]),
    ("3. Results", [
        "Of 12,438 participants included in the analytic sample, 6,712 were women and the mean age "
        "was 54.3 years. During 116,918 person years of follow up, 1,047 major adverse cardiovascular "
        "events were recorded. Participants reporting five hours of sleep or less were more likely to "
        "be male, to smoke, to have lower educational attainment, and to report depressive symptoms "
        "at baseline compared with the reference group.",

        "In the minimally adjusted model, both short and long sleep were associated with elevated "
        "risk. After full adjustment, the hazard ratio for five hours or less was 1.34 with a 95 "
        "percent confidence interval of 1.18 to 1.52, and for ten hours or more it was 1.28 with a "
        "confidence interval of 1.09 to 1.51. The six hour and nine hour categories showed "
        "intermediate estimates that did not reach conventional statistical significance.",

        "Restricting the analysis to participants free of depressive symptoms at baseline attenuated "
        "but did not eliminate the association for short sleep, with a hazard ratio of 1.21. "
        "Sensitivity analyses excluding events occurring in the first two years of follow up "
        "produced materially similar results, arguing against reverse causation as the sole "
        "explanation.",
    ]),
    ("4. Discussion", [
        "In this large prospective cohort, we observed a U shaped association between self reported "
        "sleep duration and incident cardiovascular events, with the nadir at seven to eight hours "
        "per night. The findings were robust to adjustment for a comprehensive set of confounders "
        "and persisted in several sensitivity analyses.",

        "The mechanisms linking short sleep to cardiovascular risk are plausibly bidirectional. "
        "Experimental evidence supports sympathetic activation, blunted nocturnal blood pressure "
        "dipping, and impaired glucose tolerance as intermediate pathways. Long sleep, by contrast, "
        "may reflect underlying inflammatory burden or preclinical disease, consistent with the "
        "observation that its association weakened after excluding early events.",

        "Several limitations warrant consideration. Sleep duration was self reported and measured at "
        "a single time point, which precludes assessment of change over time and introduces "
        "measurement error. Residual confounding by unmeasured factors such as shift work, noise "
        "exposure, and diagnosed sleep disorders cannot be excluded. Finally, the cohort was "
        "predominantly of European ancestry, limiting generalisability to other populations.",
    ]),
]

REFERENCES = [
    "1. Smith JA, Lee K, et al. Sleep duration and incident hypertension. J Hypertens. 2019;37:1123-1131.",
    "2. Garcia M, Chen Y. Nocturnal blood pressure patterns. Am J Cardiol. 2020;125:884-890.",
    "3. Thompson R, et al. Sleep and inflammation markers. Sleep. 2021;44:zsaa123.",
    "4. Wang L, Ito S. Shift work and cardiovascular outcomes. Occup Environ Med. 2018;75:441-448.",
    "5. Patel SR, Hu FB. Short sleep as a risk factor. Obesity. 2019;27:1021-1029.",
]


def wrap(text: str, width: float, fs: float) -> list[str]:
    """按实际字宽断行（helv 字体）"""
    words = text.split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if pymupdf.get_text_length(trial, fontname="helv", fontsize=fs) <= width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


class Writer:
    def __init__(self):
        self.doc = pymupdf.open()
        self.page_no = 0
        self.col = 0
        self.y = TOP_Y
        self._new_page()

    def _new_page(self):
        self.page_no += 1
        page = self.doc.new_page(width=PW, height=PH)
        page.insert_text((MARGIN, 30), "Journal of Test Medicine   Vol. 12, No. 3",
                         fontsize=7.5, fontname="helv", color=(0.45, 0.45, 0.45))
        page.insert_text((PW - MARGIN - 16, 30), str(self.page_no),
                         fontsize=7.5, fontname="helv", color=(0.45, 0.45, 0.45))
        page.insert_text((MARGIN, PH - 30), "Downloaded from test-journal.example.org",
                         fontsize=7, fontname="helv", color=(0.55, 0.55, 0.55))
        self.page = page
        self.col = 0
        self.y = TOP_Y
        # 双栏正文的起始 y。首页有通栏摘要时会被抬高到摘要下方，
        # 保证左右两栏都从摘要底部开始排 —— 真实论文就是这么排的。
        self.body_top = TOP_Y

    def _x(self) -> float:
        return MARGIN + self.col * (COL_W + GUTTER)

    def advance(self, need: float):
        """确保当前栏还有 need 的垂直空间，不够就换栏/换页"""
        if self.y + need <= BOTTOM_Y:
            return
        if self.col == 0:
            self.col = 1
            self.y = self.body_top
        else:
            self._new_page()

    def put_lines(self, lines: list[str], fs: float = BODY_FS, lead: float = LEAD,
                  indent_first: bool = False):
        for i, line in enumerate(lines):
            self.advance(lead)
            x = self._x() + (14 if (indent_first and i == 0) else 0)
            self.page.insert_text((x, self.y), line, fontsize=fs, fontname="helv")
            self.y += lead

    def put_paragraph(self, text: str, gap_before: float = 3.0):
        """段间留白 + 首行缩进，模拟真实排版（切分算法就靠这些信号工作）"""
        self.y += gap_before
        self.put_lines(wrap(text, COL_W, BODY_FS), indent_first=True)
        self.y += LEAD * 0.55

    def put_full_width(self, text: str, fs: float, x0: float, width: float,
                       bold_gap: float = 0.0):
        """通栏内容（标题、摘要）——x 横跨中线，用来测试通栏元素处理"""
        self.y += bold_gap
        for line in wrap(text, width, fs):
            self.page.insert_text((x0, self.y), line, fontsize=fs, fontname="helv")
            self.y += fs * 1.4
        self.y += fs * 0.4


def main():
    w = Writer()

    # 首页通栏：标题 + 摘要
    w.y = TOP_Y
    w.put_full_width(TITLE, 14, MARGIN, PW - 2 * MARGIN, bold_gap=4)
    w.put_full_width("Authors: A. Researcher, B. Colleague, C. Supervisor",
                     8.5, MARGIN, PW - 2 * MARGIN, bold_gap=2)
    w.put_full_width("Abstract", 10, MARGIN, PW - 2 * MARGIN, bold_gap=8)
    w.put_full_width(ABS_TEXT, 8.5, MARGIN, PW - 2 * MARGIN)
    w.y += 10
    w.body_top = w.y          # 两栏都从摘要下方起排

    # 双栏正文
    for heading, paras in SECTIONS:
        w.advance(LEAD * 2)
        w.put_lines([heading], fs=10.5, lead=LEAD * 1.15)
        w.y += 5
        for p in paras:
            w.put_paragraph(p)

    # 参考文献（测试截断）
    w.advance(LEAD * 2)
    w.put_lines(["References"], fs=10.5, lead=LEAD * 1.15)
    w.y += 4
    for ref in REFERENCES:
        w.put_lines(wrap(ref, COL_W, 8), fs=8, lead=8 * 1.3)

    w.doc.save(str(OUT))
    w.doc.close()
    print(f"已生成：{OUT}")
    print(f"页数：{w.page_no}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
