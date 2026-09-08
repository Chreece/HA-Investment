import fs from 'node:fs';
import * as acorn from 'acorn';
import postcss from 'postcss';

const path='custom_components/investment/www/investment-panel.js';
let src=fs.readFileSync(path,'utf8');
const bad=/indicat|indicator/i;

function parse(){return acorn.parse(src,{ecmaVersion:'latest',sourceType:'module'});}
function walk(node,fn){
  if(!node||typeof node!=='object')return;
  fn(node);
  for(const value of Object.values(node)){
    if(Array.isArray(value))for(const item of value)walk(item,fn);
    else if(value&&typeof value==='object'&&typeof value.type==='string')walk(value,fn);
  }
}

const removeMethods=new Set([
  'indicationDraftFromStored','indicationPreferencesPayload','persistIndicationDraft',
  'scheduleIndicationPersistence','legalRegionForCountry','legalRegionLabel',
  'legalRegionNotice','legalRegionOptions','registerIndicatorGatePress',
  '_openIndicationCore','openIndication','reviewIndicationLegal','closeIndicationDisclaimer',
  'acceptIndicationDisclaimer','indicationDisclaimerHtml','closeIndication',
  'loadIndicationAiTasks','captureIndicationDraft','indicationAdjustedSuggestion',
  'indicationDisplayAllocation','indicationAllocationExplanation','indicationSignalText',
  'indicationLabel','runIndication','indicationModalHtml','captureViewState','restoreViewState'
]);
let edits=[];
walk(parse(),node=>{
  if(node.type==='MethodDefinition'&&removeMethods.has(node.key?.name))edits.push([node.start,node.end]);
});
for(const [start,end] of edits.sort((a,b)=>b[0]-a[0]))src=src.slice(0,start)+src.slice(end);

let lines=src.split(/(?<=\n)/).filter(line=>{
  const s=line.trim();
  if(s.startsWith('this._developerIndicatorUnlocked='))return false;
  if(s.startsWith('this._indicationOpen='))return false;
  if(s.includes('this._developerIndicatorUnlocked=!!portfolio?.developer_indicator_unlocked'))return false;
  if(s.includes('this._indicationPreferencesLoaded&&portfolio?.indication_preferences'))return false;
  if(s.startsWith('const legalRegion=String(this._portfolio?.indication_disclaimer_region'))return false;
  if(s.includes('getElementById("indication")?.addEventListener'))return false;
  if(s.includes('getElementById("indication-gate")?.addEventListener'))return false;
  if(s.includes('this._indicationDisclaimerOpen?this.indicationDisclaimerHtml()'))return false;
  if(s.includes('this._indicationOpen?this.indicationModalHtml()'))return false;
  return true;
});
src=lines.join('');
src=src.replace(/\s*&&\s*!this\._indicationOpen/g,'');
src=src.replace(/\s*clearTimeout\(this\._indicationPersistTimer\);/g,'');
src=src.replace(/\s*const viewState=this\.captureViewState\(\);/g,'');
src=src.replace(/\s*this\.restoreViewState\(viewState\);/g,'');

lines=src.split(/(?<=\n)/).map(line=>{
  if(line.includes('${this._developerIndicatorUnlocked?')&&line.includes('<button class="icon-btn ${this._incognito')){
    const a=line.indexOf('${this._developerIndicatorUnlocked?');
    const b=line.indexOf('<button class="icon-btn ${this._incognito',a);
    if(a>=0&&b>a)return line.slice(0,a)+line.slice(b);
  }
  if(line.includes('${this._developerIndicatorUnlocked?')&&line.includes('<p>${esc(this.t("freeData"))}')){
    const a=line.indexOf('${this._developerIndicatorUnlocked?');
    const b=line.indexOf('<p>${esc(this.t("freeData"))}',a);
    if(a>=0&&b>a)return line.slice(0,a)+line.slice(b);
  }
  return line;
});
src=lines.join('');
src=src.replace('.modal-backdrop:not(.add-backdrop):not(.indication-backdrop):not(.disclaimer-backdrop)', '.modal-backdrop:not(.add-backdrop)');

{
  const a=src.indexOf('    const updateLegalAcceptButton=');
  const b=src.indexOf('    root.querySelectorAll("[data-close-add]")',a);
  if(a>=0&&b>a)src=src.slice(0,a)+src.slice(b);
}

for(let pass=0;pass<10;pass++){
  const before=src;
  src=src.replace(/([,{])\s*([A-Za-z_$][\w$]*):("(?:\\.|[^"\\])*")\s*,?/g,(m,prefix,key,val)=>bad.test(key)||bad.test(val)?prefix:m);
  src=src.replace(/\{\s*,/g,'{').replace(/,\s*}/g,'}');
  if(src===before)break;
}
src=src.replace(/\s*["']\.indication[^"']*["']\s*,?/gi,'');

function findStylesTemplate(){
  let tpl=null;
  walk(parse(),node=>{
    if(tpl||node.type!=='MethodDefinition'||node.key?.name!=='styles')return;
    walk(node.value?.body,child=>{if(!tpl&&child.type==='TemplateLiteral')tpl=child;});
  });
  return tpl;
}
const tpl=findStylesTemplate();
if(tpl&&tpl.expressions.length===0&&tpl.quasis.length===1){
  const q=tpl.quasis[0];
  const raw=src.slice(q.start,q.end);
  const root=postcss.parse(raw);
  root.walkRules(rule=>{
    const keep=(rule.selectors||[]).filter(sel=>!bad.test(sel));
    if(!keep.length)rule.remove(); else rule.selectors=keep;
  });
  src=src.slice(0,q.start)+root.toString()+src.slice(q.end);
}

src=src.replace(/^.*(?:developerIndicator|developer_indicator).*\n/gmi,'');
fs.writeFileSync(path,src);
acorn.parse(src,{ecmaVersion:'latest',sourceType:'module'});
