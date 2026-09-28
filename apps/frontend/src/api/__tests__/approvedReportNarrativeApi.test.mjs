import test from 'node:test'
import assert from 'node:assert/strict'
import { getApprovedReportNarrative } from '../anxinBoardReports.js'
const session=new Map(),key='anxinboard:local-browser-session:v1'
globalThis.window={location:{href:'http://localhost/#/projects/1/board'}}
globalThis.sessionStorage={getItem:k=>session.get(k),setItem:(k,v)=>session.set(k,v)}
test('narrative GET is exact-version/hash protected, never sends a model or confirmation request',async()=>{
 session.set(key,'synthetic-session');let calls=0
 globalThis.fetch=async(url,options)=>{
  calls++;assert.equal(url,`/api/projects/1/anxin-board/reports/3/narrative?report_hash=${'a'.repeat(64)}`)
  assert.equal(options.method,'GET');assert.equal(options.body,undefined)
  assert.equal(options.headers['X-Anxin-Session'],'synthetic-session');assert.equal(options.redirect,'error');assert.equal(options.cache,'no-store')
  return new Response(JSON.stringify({content:{}}))
 }
 assert.equal((await getApprovedReportNarrative(1,3,'a'.repeat(64))).ok,true);assert.equal(calls,1)
})
test('missing session sends nothing; invalid session and network failure are not retried',async()=>{
 session.clear();let calls=0
 globalThis.fetch=async()=>{calls++;throw Error('private raw detail')}
 assert.equal((await getApprovedReportNarrative(1,3,'a'.repeat(64))).ok,false);assert.equal(calls,0)
 session.set(key,'expired');globalThis.fetch=async()=>{calls++;return new Response(JSON.stringify({detail:{code:'LOCAL_SESSION_SESSION_INVALID'}}),{status:403})}
 assert.equal((await getApprovedReportNarrative(1,3,'a'.repeat(64))).status,403);assert.equal(session.get(key),'');assert.equal(calls,1)
 session.set(key,'synthetic-session');globalThis.fetch=async()=>{calls++;throw Error('private raw detail')}
 const result=await getApprovedReportNarrative(1,3,'a'.repeat(64));assert.equal(result.ok,false);assert.deepEqual(result.body,{});assert.equal(calls,2)
})
