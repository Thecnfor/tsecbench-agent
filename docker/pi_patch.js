// Build-time patch: synthesize missing tool results for dangling assistant tool_calls
// (deepseek 400 "tool_calls must be followed by tool messages"). See THIRD_PARTY.md.
const fs = require("fs");
const path = require("path");

const SAN = `function __tsecSanitize(params){let out=[];for(let __k=0;__k<params.length;__k++){let __m=params[__k];out.push(__m);if(!(__m&&__m.role==="assistant"&&Array.isArray(__m.tool_calls)&&__m.tool_calls.length>0))continue;let __have=new Set();for(let __j=__k+1;__j<params.length;__j++){let __n=params[__j];if(__n&&__n.role==="tool"&&__n.tool_call_id){__have.add(String(__n.tool_call_id).split("|")[0]);}else if(__n&&(__n.role==="assistant"||__n.role==="user")){break;}}for(let __tc of __m.tool_calls){let __id=String(__tc&&(__tc.id||(__tc.function&&__tc.function.name))||("call_"+__k)).split("|")[0];if(!__have.has(__id)){out.push({role:"tool",tool_call_id:__id,content:"[system] tool call was interrupted before its result; continue the task."});}}}return out;}`;

function patchFile(file) {
  let src = fs.readFileSync(file, "utf8");
  if (src.includes("__tsecSanitize")) { console.log("already patched:", file); return; }
  if (!src.includes("function convertMessages")) { console.log("no convertMessages:", file); return; }
  src = src.replace("function convertMessages", SAN + "function convertMessages");
  const start = src.indexOf("function convertMessages");
  const end = src.indexOf("function convertTools", start);
  if (end < 0) throw new Error("convertTools not found in " + file);
  let body = src.slice(start, end);
  if (!body.includes("return params}")) throw new Error("return params} not found in " + file);
  body = body.replace("return params}", "return __tsecSanitize(params)}");
  src = src.slice(0, start) + body + src.slice(end);
  fs.writeFileSync(file, src);
  console.log("patched:", file);
}

const base = process.argv[2];
const targets = [];
for (const dir of [path.join(base, "dist/bundle/chunks"), path.join(base, "dist/api")]) {
  if (fs.existsSync(dir)) {
    for (const f of fs.readdirSync(dir)) {
      if (f.startsWith("openai-completions") && f.endsWith(".js")) targets.push(path.join(dir, f));
    }
  }
}
if (!targets.length) throw new Error("openai-completions chunk not found under " + base);
targets.forEach(patchFile);
