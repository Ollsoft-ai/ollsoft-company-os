import { EditorState } from "@codemirror/state";
import { markdown } from "@codemirror/lang-markdown";
import { ensureSyntaxTree } from "@codemirror/language";
function mkdoc(n, t){let s="# Long\n\n";for(let i=0;i<n;i++)s+=`filler *paragraph* line with a [link](x.md) and \`code\` ${i}\n\n`;if(t)s+="| a | b |\n| --- | --- |\n| 1 | 2 |\n\n";return s;}
function run(nParas, withTable){
  const doc=mkdoc(nParas,withTable);
  const state=EditorState.create({doc,extensions:[markdown()]});
  const tree=ensureSyntaxTree(state,state.doc.length,30000);
  let nodes=0; tree.iterate({enter:()=>{nodes++;}});
  const body=()=>{const r=[];tree.iterate({enter:(n)=>{if(n.name!=="Table")return;const f=state.doc.lineAt(n.from).from,to=state.doc.lineAt(n.to).to;r.push([f,to,state.sliceDoc(f,to)]);return false;}});return r.length;};
  for(let k=0;k<2000;k++) body();               // warm
  const N=2000,t0=performance.now();
  for(let k=0;k<N;k++) body();
  const t1=performance.now();
  console.log(`paras=${String(nParas).padStart(6)} chars=${state.doc.length} nodes=${nodes} tables=${body()} perCall=${((t1-t0)/N*1000).toFixed(1)}us`);
}
for (const n of [400,2000,10000,40000]) run(n,true);
run(400,false);
