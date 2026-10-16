import http from 'node:http';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import net from 'node:net';
import {spawn} from 'node:child_process';

const config=JSON.parse(await fs.readFile('/run/config/browser.json','utf8'));
const cli='/opt/browser/node_modules/@playwright/cli/playwright-cli.js';
let busy=false;
function validURL(value){
 if(typeof value!=='string'||value.length>4096||/[\u0000-\u0020]/.test(value))throw Error('Invalid URL');
 const u=new URL(value);if(!['https:','http:'].includes(u.protocol)||u.username||u.password||u.hash||!['','80','443'].includes(u.port))throw Error('Unsupported URL');
 const host=u.hostname.toLowerCase().replace(/\.$/,'');
 if(host==='localhost'||host==='ttlouis.space'||/\.(localhost|local|internal|ttlouis\.space)$/.test(host))throw Error('Private host');
 if(host.includes(':'))throw Error('Literal IPv6 destinations are not supported');
 if(net.isIP(host)===4){const a=host.split('.').map(Number);if(a[0]===0||a[0]===10||a[0]===127||a[0]>=224||(a[0]===100&&a[1]>=64&&a[1]<=127)||(a[0]===169&&a[1]===254)||(a[0]===172&&a[1]>=16&&a[1]<=31)||(a[0]===192&&(a[1]===0||a[1]===168))||(a[0]===198&&(a[1]===18||a[1]===19)))throw Error('Private IP')}
 return u.href;
}
function command(args,cwd,timeout=40000){return new Promise((resolve,reject)=>{
 const child=spawn(process.execPath,[cli,...args],{cwd,env:{...process.env,NO_UPDATE_NOTIFIER:'1',CI:'1'},stdio:['ignore','pipe','pipe']});
 let stdout='',stderr='',exceeded=false;
 const timer=setTimeout(()=>child.kill('SIGKILL'),timeout);
 child.stdout.on('data',data=>{stdout+=data;if(stdout.length>30*1024*1024){exceeded=true;child.kill('SIGKILL')}});
 child.stderr.on('data',data=>{stderr+=data;if(stderr.length>65536){exceeded=true;child.kill('SIGKILL')}});
 child.on('error',reject);child.on('close',code=>{clearTimeout(timer);code||exceeded?reject(Error('Browser command failed')):resolve(stdout)});
});}
async function perform(p){
 if(!['search','read','download'].includes(p.operation))throw Error('Invalid operation');
 let target;
 if(p.operation==='search'){
  if(typeof p.query!=='string'||!p.query.trim()||p.query.length>300)throw Error('Invalid search');
  target='https://www.bing.com/search?q='+encodeURIComponent(p.query);
 }else target=validURL(p.url);
 const dir=await fs.mkdtemp(path.join(os.tmpdir(),'web-'));
 const session='web-'+crypto.randomBytes(10).toString('hex');
 const prefix=['-s='+session,'--raw'];
 try{
  const cfg={browser:{browserName:'chromium',isolated:true,launchOptions:{headless:true,chromiumSandbox:false,proxy:{server:'http://hermes-talk-web-egress:9121',bypass:'<-loopback>'},args:['--disable-quic','--disable-background-networking','--disable-component-update','--disable-sync']},contextOptions:{serviceWorkers:'block',acceptDownloads:false}},timeouts:{navigation:20000,action:10000},network:{blockedOrigins:[]}};
  await fs.writeFile(path.join(dir,'config.json'),JSON.stringify(cfg));
  await command([...prefix,'open','--config='+path.join(dir,'config.json'),'--idle-timeout=60000'],dir,20000);
  // Fixed host-authored code. Neither page text nor model input becomes code.
  const program=`async page => {
   const input=${JSON.stringify({operation:p.operation,target})};
   let requests=0;
   await page.context().route('**/*',route=>{
    const req=route.request();const url=new URL(req.url());
    if(++requests>80||!['http:','https:'].includes(url.protocol)||!['GET','HEAD'].includes(req.method())||['image','media','font'].includes(req.resourceType()))return route.abort();
    return route.continue();
   });
   await page.context().routeWebSocket('**/*',socket=>socket.close());
   if(input.operation==='download'){
    const response=await page.request.get(input.target,{timeout:30000,maxRedirects:6});
    if(!response.ok())throw Error('Download HTTP '+response.status());
    const headers=response.headers();const mime=(headers['content-type']||'').split(';')[0].toLowerCase();
    const permitted=['application/pdf','text/plain','text/csv','application/json','image/png','image/jpeg','image/webp'];
    if(!permitted.includes(mime)||Number(headers['content-length']||0)>20971520)throw Error('Unsupported or oversized download');
    const data=await response.body();if(data.length>20971520)throw Error('Download too large');
    return {url:response.url(),mime,bytes:data.length,data:data.toString('base64')};
   }
   const navigation=await page.goto(input.target,{waitUntil:'domcontentloaded',timeout:20000});
   if(!navigation||!navigation.ok())throw Error('Navigation HTTP failure');
   await page.waitForTimeout(700);
   return await page.evaluate(operation=>{
    const links=Array.from(document.querySelectorAll('a[href]')).map(a=>({title:(a.innerText||a.textContent||'').trim().slice(0,180),url:a.href})).filter(a=>/^https?:/.test(a.url)&&a.url.length<=4096);
    const results=operation==='search'?Array.from(document.querySelectorAll('#b_results .b_algo')).slice(0,8).map(el=>({title:el.querySelector('h2')?.innerText?.slice(0,180),url:el.querySelector('h2 a')?.href,excerpt:el.querySelector('p')?.innerText?.slice(0,700)})).filter(x=>x.url):undefined;
    const text=(document.querySelector('main,article,[role="main"]')||document.body)?.innerText||'';
    return {url:location.href,title:document.title.slice(0,250),text:text.slice(0,16000),truncated:text.length>16000,links:links.slice(0,60),results};
   },input.operation);
  }`;
  const file=path.join(dir,'operation.js');await fs.writeFile(file,program);
  const result=await command([...prefix,'run-code','--filename='+file],dir,45000);
  const parsed=JSON.parse(result);validURL(parsed.url);
  return parsed;
 }finally{
  try{await command([...prefix,'close'],dir,8000)}catch{
   // Only this isolated sidecar uses this private CLI registry; one operation
   // owns it at a time. Stop any daemon left behind by a timed-out close.
   try{await command(['kill-all'],dir,8000)}catch{}
  }
  await fs.rm(dir,{recursive:true,force:true});
 }
}
http.createServer(async(req,res)=>{
 const reply=(status,data)=>{res.writeHead(status,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(data))};
 if(req.url==='/health'&&req.method==='GET')return reply(200,{ok:true});
 const token=(req.headers.authorization||'').replace(/^Bearer /,'');
 const given=Buffer.from(token);
 const tenant=config.tenants.find(t=>given.length===Buffer.byteLength(t.token)&&crypto.timingSafeEqual(given,Buffer.from(t.token)));
 if(!tenant)return reply(401,{error:'Unauthorized'});
 if(req.url!=='/web'||req.method!=='POST')return reply(404,{});
 if(busy)return reply(429,{error:'Browser busy'});
 busy=true;
 try{
  let body='';for await(const part of req){body+=part;if(body.length>16384)throw Error('Body too large')}
  const p=JSON.parse(body);const out=await perform(p);reply(200,out);
 }catch{reply(502,{error:'Public browser request failed or destination blocked'})}
 finally{busy=false}
}).listen(9120,'0.0.0.0');
