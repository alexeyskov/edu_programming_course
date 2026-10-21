"""Zero-point grading acceptance, isolated demo API and real Chromium only.

Start with VITE_DEMO_MODE=always VITE_API_BASE=/__zero-score-audit-api npm run dev
-- --host 127.0.0.1 --port 5200. No deployed API, Moodle or model is contacted.
"""

import argparse
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright, expect


MOCK_API = """async mode => {
  const {api} = await import('/src/lib/api.ts');
  const source = await api.getSubmission('sub-1');
  const group = {id:'zero-smoke',title:'Проверка без балла и с баллом',items:[
    {submissionId:'zero',position:1,title:'Лабораторная',maxScore:0,status:'UNGRADED'},
    {submissionId:'positive',position:2,title:'Контрольная',maxScore:5,status:'UNGRADED'}
  ]};
  const questions = Object.fromEntries(group.items.map(item => [item.submissionId,{
    ...source,id:item.submissionId,assessmentId:'zero-smoke-assessment',courseId:'demo-course',
    assessmentTitle:item.title,taskStatement:'Проверьте код и оставьте комментарий.',
    status:'UNGRADED',score:undefined,maxScore:item.maxScore,claim:undefined,
    latestDecision:undefined,decisionHistory:[],history:[],reviewGroup:group,
    canReview:mode!=='read-only',reviewRequired:true,
    files:[{id:'main',path:'main.cpp',language:'cpp',content:'int main() { return 0; }'}]
  }]));
  const drafts = {};
  const claim = id => ({id:'claim-'+id,ownerId:'teacher-demo',ownerName:'Преподаватель',mine:true,
    expiresAt:new Date(Date.now()+3600000).toISOString()});
  window.reviewAudit = {saved:[],finalized:[],claimed:[]};
  api.queryCodeIntelligence = async () => ({result:null,diagnostics:[]});
  api.getSubmission = async id => structuredClone(questions[id]);
  api.getAssessment = async () => ({id:'zero-smoke-assessment',courseId:'demo-course',reviewRequired:true,decisionSupportEnabled:true});
  api.getReviewDraft = async id => structuredClone(drafts[id] ?? null);
  api.claimSubmission = async id => {
    window.reviewAudit.claimed.push(id); questions[id].claim=claim(id); questions[id].status='CLAIMED';
    return structuredClone(questions[id].claim);
  };
  api.heartbeatClaim = async id => claim(id.replace(/^claim-/,''));
  api.releaseClaim = async claimId => {
    const item = questions[claimId.replace(/^claim-/,'')];
    item.claim=undefined; item.status='UNGRADED';
  };
  api.saveReview = async (id,grade,comment,aiReviewId) => {
    window.reviewAudit.saved.push({id,grade,comment,...(aiReviewId ? {aiReviewId} : {})});
    return drafts[id]={grade,comment,aiReviewId};
  };
  api.finalizeReview = async (id,grade,comment,aiReviewId) => {
    window.reviewAudit.finalized.push({id,grade,comment,...(aiReviewId ? {aiReviewId} : {})});
    return {id:'decision-'+id,lmsExportState:'PENDING'};
  };
  api.getEvidenceRuns = api.getAuthorshipAnalyses = api.getSimilarityAnalyses =
    api.getTeacherAiThreads = api.getAiMessages = async () => [];
  api.getAiCodeReview = async id => mode==='ai' && id==='zero'
    ? {id:'zero-ai-report',enabled:true,state:'COMPLETED',recommendationStatus:'PENDING',
       automatic:true,summary:'Анализ выполнен.',findings:[],recommendedGrade:0,
       recommendedComment:'Комментарий ИИ без числовой оценки.'}
    : {enabled:true,state:'NOT_STARTED',findings:[],summary:''};
  api.discardAiCodeReview = async () => ({enabled:true,state:'NOT_STARTED',findings:[],summary:''});
}"""


async def main(base_url, output):
    screenshots = Path(output)
    screenshots.mkdir(parents=True, exist_ok=True)
    errors, unexpected, checked = [], [], []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            for mobile in (False, True):
                for mode in ("manual", "ai", "read-only"):
                    context = await browser.new_context(
                        viewport={"width": 390, "height": 844} if mobile else {"width": 1440, "height": 1000},
                        is_mobile=mobile, has_touch=mobile,
                    )

                    async def isolate(route):
                        url = route.request.url
                        if not url.startswith(base_url + "/") or "/__zero-score-audit-api/" in url or "/api/v1/" in url:
                            unexpected.append(url)
                            await route.abort()
                        else:
                            await route.continue_()

                    await context.route("**/*", isolate)
                    page = await context.new_page()
                    page.set_default_timeout(15000)
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    await page.goto(base_url + "/login")
                    await page.get_by_role("button", name="Преподаватель", exact=True).click()
                    await page.get_by_role("button", name="Открыть демо", exact=True).click()
                    await page.locator(".page-heading").first.wait_for()
                    await page.evaluate(MOCK_API, mode)
                    await page.evaluate("""() => {
                      history.pushState(null,'','/review/zero'); dispatchEvent(new PopStateEvent('popstate'));
                    }""")
                    if mobile:
                        await expect(page.locator(".mobile-code-preview")).to_be_attached()
                        assert await page.locator(".review-editor textarea, .review-editor [contenteditable]").count() == 0
                    else:
                        await page.locator(".monaco-editor").first.wait_for()

                    async def open_form(claim=True):
                        if mobile:
                            await page.get_by_role("button", name="Задание", exact=True).click()
                            begin = page.get_by_role("button", name="Начать проверку", exact=True)
                            if claim and await begin.is_visible():
                                await begin.click()
                        await expect(page.get_by_label("Итоговый балл", exact=True)).to_be_visible()

                    await open_form(mode != "read-only")
                    grade = page.get_by_label("Итоговый балл", exact=True)
                    comment = page.get_by_label("Комментарий студенту", exact=True)
                    await expect(grade).to_be_disabled()
                    await expect(grade).to_have_value("")
                    await expect(grade).to_have_attribute("aria-describedby", "review-points-disabled-hint")
                    await expect(page.locator("#review-points-disabled-hint")).to_have_text("Баллы за эту работу не выставляются.")
                    await expect(page.get_by_role("tab", name="№1 Лабораторная", exact=False)).to_contain_text("Без оценки")
                    await grade.evaluate("element => element.focus()")
                    assert not await grade.evaluate("element => element === document.activeElement")
                    assert "0/0" not in await page.locator("body").inner_text()
                    if mode == "read-only":
                        await expect(comment).to_be_disabled()
                        assert await page.evaluate("window.reviewAudit.claimed.length") == 0
                    else:
                        await expect(comment).to_be_enabled()
                        if mode == "manual":
                            await comment.fill("Замечание к лабораторной без баллов.")
                            await page.get_by_role("button", name="Сохранить черновик", exact=True).click()
                            await expect(page.get_by_role("button", name="Сохранить черновик", exact=True)).to_be_disabled()
                            assert await page.evaluate("window.reviewAudit.saved") == [
                                {"id": "zero", "grade": 0, "comment": "Замечание к лабораторной без баллов."}
                            ]
                            await page.get_by_role("tab", name="№2 Контрольная", exact=False).click()
                            await expect(page).to_have_url(base_url + "/review/positive")
                            await open_form()
                            await expect(grade).to_be_enabled()
                            await expect(grade).to_have_value("")
                            await grade.fill("2")
                            await comment.fill("Контрольная на два балла.")
                            await page.get_by_role("tab", name="№1 Лабораторная", exact=False).click()
                            await expect(page).to_have_url(base_url + "/review/zero")
                            await open_form()
                            await expect(grade).to_be_disabled()
                            await expect(grade).to_have_value("")
                            await expect(comment).to_have_value("Замечание к лабораторной без баллов.")
                            saved = await page.evaluate("window.reviewAudit.saved")
                            assert saved[-1] == {"id": "positive", "grade": 2, "comment": "Контрольная на два балла."}, saved
                            final_comment = "Лабораторная проверена, без оценки."
                            await comment.fill(final_comment)
                        else:
                            await page.get_by_role("button", name="Поставить комментарий от ИИ", exact=True).click()
                            final_comment = "Комментарий ИИ без числовой оценки."
                            await expect(comment).to_have_value(final_comment)
                            assert await page.evaluate("window.reviewAudit.finalized.length") == 0
                            await expect(grade).to_be_disabled()
                            await expect(grade).to_have_value("")
                        await page.screenshot(path=str(screenshots / f"{'mobile' if mobile else 'desktop'}-{mode}.png"), full_page=True)
                        await page.locator(".grading-panel").get_by_role("button", name="Утвердить", exact=True).click()
                        dialog = page.get_by_role("dialog")
                        await expect(dialog).to_contain_text("Без оценки")
                        await expect(dialog).to_contain_text(final_comment)
                        assert "0/0" not in await dialog.inner_text()
                        await dialog.get_by_role("button", name="Утвердить и отправить", exact=True).click()
                        await expect(page).to_have_url(base_url + "/submissions")
                        expected = {"id": "zero", "grade": 0, "comment": final_comment}
                        if mode == "ai":
                            expected["aiReviewId"] = "zero-ai-report"
                        assert await page.evaluate("window.reviewAudit.finalized") == [expected]
                    if mobile:
                        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                    checked.append({"mobile": mobile, "mode": mode})
                    await context.close()
        finally:
            await browser.close()
    assert not errors, errors
    assert not unexpected, unexpected
    print(json.dumps({"ok": True, "checked": checked, "page_errors": errors, "external_requests": unexpected}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:5200")
    parser.add_argument("--output", default="/tmp/eduprog-zero-score-review")
    args = parser.parse_args()
    asyncio.run(main(args.base_url, args.output))
