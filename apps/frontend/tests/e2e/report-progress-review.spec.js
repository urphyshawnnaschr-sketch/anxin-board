import { test, expect } from '@playwright/test'
async function open(page, { preview = true, approved = false } = {}) {
  const writes = []
  const rows = [{module_id:'A',name:'功能甲',previous_stage:'开发中',stage:'已完成',evidence_ids:['git1']},{module_id:'B',name:'功能乙',previous_stage:'已完成',stage:'已完成',evidence_ids:[]}]
  await page.route('**/api/**', route => {
    const req=route.request(), path=new URL(req.url()).pathname
    if(!path.startsWith('/api/')) return route.continue()
    if(req.method()!=='GET'){writes.push(path);return route.fulfill({status:409,json:{detail:{code:'SYNTHETIC_STOP'}}})}
    if(path==='/api/projects/4') return route.fulfill({json:{id:4,name:'累计进度测试',status:'active',version:1,branch:'main',git_url:'https://example.invalid/test.git'}})
    if(path.endsWith('/report-review/current')) return route.fulfill({json:{project_id:4,report_version:{project_id:4,report_version_id:9,state_version:1,version_no:1,lifecycle:approved?'approved':'pending_review',report_content_hash:'a'.repeat(64)},ai_raw:{content:{plain_summary:'本次增加检查记录'}},evidence_refs:[{type:'frozen_reference',evidence_id:'fake'}],...(preview?{progress_preview:typeof preview==='object'?preview:{state:'ready',modules:rows,evidence_refs:[{type:'git_file_fact',evidence_id:'git1',path:'src/module.ts'}]}}:{})}})
    return route.fulfill({status:404,json:{}})
  })
  await page.goto('/#/projects/4/review')
  await expect(page.getByText('本次增加检查记录')).toBeVisible()
  return writes
}
test('complete table separates activity, corrections opt in and reload clears them',async({page})=>{
 const writes=await open(page)
 const table=page.getByTestId('progress-review')
 await expect(table.locator('tbody tr')).toHaveCount(2)
 await expect(table.getByText('未涉及的功能沿用上次进度。', {exact:false})).toBeVisible()
 const first=table.locator('tbody tr').first()
 await expect(first.getByRole('checkbox',{name:'更正状态'})).not.toBeChecked()
 await first.getByRole('checkbox',{name:'更正状态'}).check()
 await first.getByLabel('更正原因（必填）').fill('代码已核实回退')
 await first.getByLabel('确认后的状态').selectOption('开发中')
 await expect(first.getByRole('checkbox',{name:'src/module.ts'})).toBeVisible()
 await expect(first.getByRole('checkbox')).toHaveCount(2)
 await page.getByRole('button',{name:'刷新状态'}).click()
 await expect(page.getByTestId('progress-review').getByRole('checkbox',{name:'更正状态'}).first()).not.toBeChecked()
 expect(writes).toEqual([])
})
test('legacy absence adds no fake table and unavailable remains explicit',async({page})=>{
 const writes=await open(page,{preview:{state:'unavailable',message:'代码范围未接续，请重新读取。'}})
 await expect(page.getByText('代码范围未接续，请重新读取。')).toBeVisible()
 await expect(page.getByTestId('progress-review')).toHaveCount(0)
 expect(writes).toEqual([])
})
test('approved report permits no cumulative corrections',async({page})=>{
 const writes=await open(page,{approved:true})
 await expect(page.getByTestId('progress-review')).toBeVisible()
 await expect(page.getByTestId('progress-review').getByRole('checkbox')).toHaveCount(0)
 expect(writes).toEqual([])
})
