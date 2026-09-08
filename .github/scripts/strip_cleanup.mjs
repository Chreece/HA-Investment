import fs from 'node:fs';
import * as acorn from 'acorn';

const path='custom_components/investment/www/investment-panel.js';
let src=fs.readFileSync(path,'utf8');
const bad=/indication|developerIndicator|developer_indicator|indicator-gate/i;

function parse(){return acorn.parse(src,{ecmaVersion:'latest',sourceType:'module'});}

// Remove top-level translation patch constants that are wholly feature-specific,
// while preserving mixed patches such as R4_I18N (shared ownership + optional menu).
{
  const ast=parse();
  const removedNames=new Set();
  const edits=[];
  for(const node of ast.body){
    if(node.type!=='VariableDeclaration')continue;
    const names=node.declarations.map(d=>d.id?.name).filter(Boolean);
    const text=src.slice(node.start,node.end);
    for(const name of names){
      if(name!=='R4_I18N' && bad.test(text)){
        removedNames.add(name);
        edits.push([node.start,node.end]);
        break;
      }
    }
  }
  for(const node of ast.body){
    if(node.type==='VariableDeclaration')continue;
    const text=src.slice(node.start,node.end);
    if([...removedNames].some(name=>text.includes(name)))edits.push([node.start,node.end]);
  }
  for(const [start,end] of edits.sort((a,b)=>b[0]-a[0]))src=src.slice(0,start)+src.slice(end);
}

// Repeatedly prune feature-specific string properties from mixed language maps.
for(let pass=0;pass<30;pass++){
  const before=src;
  src=src.replace(/(?<=\{|,)\s*([A-Za-z_$][\w$]*):("(?:\\.|[^"\\])*")\s*,?/g,(m,key,val)=>bad.test(key)||bad.test(val)?'':m);
  src=src.replace(/\{\s*,/g,'{').replace(/,\s*}/g,'}');
  if(src===before)break;
}

// Two bind lines precede the larger removed control block in the shared method.
src=src.split(/(?<=\n)/).filter(line=>{
  const s=line.trim();
  if(s.startsWith('root.') && /data-indication|closeIndication|openIndication|reviewIndication/i.test(s))return false;
  return true;
}).join('');

fs.writeFileSync(path,src);
acorn.parse(src,{ecmaVersion:'latest',sourceType:'module'});
