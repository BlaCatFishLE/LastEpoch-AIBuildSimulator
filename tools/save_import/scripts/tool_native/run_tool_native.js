#!/usr/bin/env node
// Formal offline adapter for lastepochsupertools v1.3.1.
// Inputs: --save, --tool-home, --out. External tool code remains in the user's installation.
// Origins and limitations: docs/contracts/save-onboarding.md.
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const args = process.argv.slice(2);
function option(name) {
  const i = args.indexOf(name);
  if (i < 0 || !args[i + 1]) throw new Error('missing ' + name);
  return path.resolve(args[i + 1]);
}
const TOOL = option('--tool-home');
const SAVE = option('--save');
const OUT = option('--out');
fs.mkdirSync(OUT, { recursive: true });
const PLANNER_JS = path.join(TOOL, 'planner', 'js', 'planner.js');
const DB_JS = [
  path.join(TOOL, 'data', 'version150', 'db', 'js', '08266c841e3045b3cb400be73fd0f879.js'),
  path.join(TOOL, 'data', 'version150', 'db', 'js', 'e3e0b655a8f4f31d04f48451cb59f5cd.js'),
];

const plannerText = fs.readFileSync(PLANNER_JS, 'utf8');

/* ------------------------------------------------------------------ extraction */
// Slice `function NAME(` ... matching closing brace; strings/template literals/regex-ish skipped.
function extractFunction(src, name) {
  const re = new RegExp('function\\s+' + name.replace(/[$]/g, '\\$') + '\\s*\\(');
  const m = re.exec(src);
  if (!m) throw new Error('function not found: ' + name);
  const braceStart = src.indexOf('{', m.index);
  let depth = 0, i = braceStart;
  let inStr = null, esc = false;
  for (; i < src.length; i++) {
    const c = src[i];
    if (inStr) {
      if (esc) { esc = false; continue; }
      if (c === '\\') { esc = true; continue; }
      if (c === inStr) inStr = null;
      continue;
    }
    if (c === '"' || c === "'" || c === '`') { inStr = c; continue; }
    if (c === '{') depth++;
    else if (c === '}') { depth--; if (depth === 0) { i++; break; } }
  }
  return src.slice(m.index, i);
}

// Slice `var NAME=function(){...}()` as one complete statement (LZ-string).
function extractVarIife(src, varName) {
  const needle = 'var ' + varName + '=function(){';
  const start = src.indexOf(needle);
  if (start < 0) throw new Error('var iife not found: ' + varName);
  const braceStart = src.indexOf('{', start);
  let depth = 0, i = braceStart;
  let inStr = null, esc = false;
  for (; i < src.length; i++) {
    const c = src[i];
    if (inStr) {
      if (esc) { esc = false; continue; }
      if (c === '\\') { esc = true; continue; }
      if (c === inStr) inStr = null;
      continue;
    }
    if (c === '"' || c === "'" || c === '`') { inStr = c; continue; }
    if (c === '{') depth++;
    else if (c === '}') { depth--; if (depth === 0) { i++; break; } }
  }
  // expect the immediately-called IIFE tail: "()" possibly with ";"
  let j = i;
  while (j < src.length && /\s/.test(src[j])) j++;
  if (src.startsWith('()', j)) { j += 2; while (j < src.length && /\s/.test(src[j])) j++; if (src[j] === ';') j++; }
  else throw new Error('unexpected tail for ' + varName + ': ' + JSON.stringify(src.slice(i, i + 20)));
  return src.slice(start, j);
}

function extractConst(src, needle) {
  const start = src.indexOf(needle);
  if (start < 0) throw new Error('const not found: ' + needle);
  const semi = src.indexOf(';', start);
  return src.slice(start, semi + 1);
}

const FUNCS = [
  'pvmBuildAffixRev', 'pvmDecodeItem', 'pvmReadAffsGen', 'pvmSepCorrupt',
  'pvmDecodeS4', 'pvmDecodeS5', 'pvmBuildWireFromSave', 'pvmEncodeItem',
];
const code = {
  pvmtb: extractVarIife(plannerText, 'pvmtb'),
  idEncoders: [
    extractFunction(plannerText, 'gb'),
    extractFunction(plannerText, 'Dc'),
    extractFunction(plannerText, 'Ne'),
    extractFunction(plannerText, 'Oe'),
    extractFunction(plannerText, 'ec'),
  ].join('\n'),
  leCid2Slot: extractConst(plannerText, 'var LE_CID2SLOT='),
  funcs: 'var pvmAffixRev=null;\n' + FUNCS.map((n) => extractFunction(plannerText, n)).join('\n'),
};

/* ------------------------------------------------------------------ sandbox */
const sandbox = {};
sandbox.window = sandbox;           // browser-ish: window === global
sandbox.globalThis = sandbox;
sandbox.self = sandbox;
sandbox.console = console;
sandbox.setTimeout = setTimeout;
sandbox.clearTimeout = clearTimeout;
sandbox.document = { ready: function () {} };
sandbox.$ = function () { return { ready: function () {}, on: function () {}, find: function () { return []; }, length: 0 }; };
vm.createContext(sandbox, { name: 'lastepochsupertools' });

function run(src, name) {
  return vm.runInContext(src, sandbox, { filename: name, timeout: 120000 });
}

console.log('[1] loading item DB ...');
for (const f of DB_JS) {
  run(fs.readFileSync(f, 'utf8'), path.basename(f));
}
if (!sandbox.window.itemDB) throw new Error('window.itemDB not defined after DB load');
console.log('    itemDB loaded:',
  'equippable=' + Object.keys(sandbox.window.itemDB.itemList.equippable).length,
  'nonEquippable=' + Object.keys(sandbox.window.itemDB.itemList.nonEquippable).length,
  'uniques=' + Object.keys(sandbox.window.itemDB.uniqueList.uniques).length,
  'singleAffixes=' + Object.keys(sandbox.window.itemDB.affixList.singleAffixes).length,
  'multiAffixes=' + Object.keys(sandbox.window.itemDB.affixList.multiAffixes).length);

console.log('[2] installing tool code (LZ-string + id encoders + reader/writer functions) ...');
run(code.pvmtb, 'pvmtb.js');
run(code.idEncoders, 'id-encoders.js');
run(code.leCid2Slot, 'le-save-constants.js');
run(code.funcs, 'planner-functions.js');
for (const n of FUNCS) {
  if (typeof sandbox[n] !== 'function') throw new Error('function did not install: ' + n);
}
console.log('    installed:', FUNCS.join(', '));

console.log('[3] bootstrap tool globals (pvmj/pvmgb/pvmk/pvml/$db/Ca) ...');
// Mirrors what planner.js $db.init() builds, using the tool's own encoders.
run(`
(function () {
  var db = window.itemDB;
  var Ca = {}, pvmj = {}, pvmgb = {}, pvmk = {}, pvml = {}, pvmjb = {};
  function each(o, fn) { if (!o) return; Object.keys(o).forEach(function (k) { fn(k, o[k]); }); }
  ['equippable', 'nonEquippable'].forEach(function (grp) {
    each(db.itemList[grp], function (btId, bt) {
      Ca[bt.baseTypeId] = bt;
      each(bt.subItems, function (stId, st) {
        st.baseTypeId = bt.baseTypeId;
        st.rarity = ec();               // tool: ec() -> 0
        st.id = Dc(st);                 // tool: Dc(t)
        pvmj[st.id] = st;
        if (st.isLegacySubType) pvmgb[st.id] = st;
      });
    });
  });
  each(db.uniqueList.uniques, function (k, m) {
    m.rarity = m.isLegendary ? 9 : (m.isSetItem ? 8 : 7);   // tool: $db.init
    var subs = m.subTypeIds || [];
    if (subs.length === 1) {
      m.subTypeId = subs[0];
      m.id = Ne(m);
      pvmgb[m.id] = m;
    } else {
      // multi-subtype unique: the game defines one encoded id per subtype; keep clone per subtype
      subs.forEach(function (st) {
        var c = Object.assign({}, m);
        c.subTypeId = st;
        c.id = Ne(c);
        pvmgb[c.id] = c;
      });
    }
  });
  each(db.affixList.singleAffixes, function (k, m) { m.id = Oe(m); if (m.type === 0) pvmk[m.id] = m; else if (m.type === 1) pvml[m.id] = m; });
  each(db.affixList.multiAffixes,  function (k, m) { m.id = Oe(m); if (m.type === 0) pvmk[m.id] = m; else if (m.type === 1) pvml[m.id] = m; });
  each(db.setBonusList, function (k, m) { pvmjb[m.setID] = m; });
  window.pvmj = pvmj; window.pvmgb = pvmgb; window.pvmk = pvmk; window.pvml = pvml;
  window.pvmjb = pvmjb; window.Ca = Ca;
  window.$db = {
    getItemById: function (id) { return pvmj[id] || pvmgb[id] || null; },
    getIdolSize: function (item) {
      var bt = Ca[item && item.baseTypeId];
      return bt && bt.size ? { x: bt.size.x, y: bt.size.y } : { x: 1, y: 1 };
    }
  };
  window.gameVersion = 'Version 1.5.2';
  window.LEWeaverTree = null;
  window.__bootstrap = {
    pvmj: Object.keys(pvmj).length, pvmgb: Object.keys(pvmgb).length,
    pvmk: Object.keys(pvmk).length, pvml: Object.keys(pvml).length,
    Ca: Object.keys(Ca).length
  };
  // reset cached affix reverse map so it is rebuilt from the bootstrapped tables
  if (typeof pvmAffixRev !== 'undefined') pvmAffixRev = null;
})();
`, 'bootstrap.js');
console.log('    bootstrap:', JSON.stringify(sandbox.__bootstrap));

/* id-encoder self-check against known planner ids from the archived planner build */
console.log('[4] id-encoder self-check vs archived planner build ids ...');
run(`
window.__idcheck = (function () {
  var out = {};
  function chk(name, got, want) { out[name] = { got: got, want: want, ok: got === want }; }
  // head: unique 316 subtype 23 -> UAwJgzGCMBsQ (build-response equipment.head.id)
  chk('U_316_23', Ne({ subTypeId: 23, uniqueId: 316 }), 'UAwJgzGCMBsQ');
  // waist: base type 2 subtype 6 rarity 0 uniqueId 0 -> IIwBhCYwNjEg (equipment.waist.id)
  chk('I_2_6', Dc({ baseTypeId: 2, subTypeId: 6, rarity: 0, uniqueId: 0 }), 'IIwBhCYwNjEg');
  // affix 13 -> AAwRgzEA (equipment.head.affixes[0].id)
  chk('A_13', Oe({ affixId: 13 }), 'AAwRgzEA');
  // affix 100 -> AIwBhQ (equipment.waist ordinary affix)
  chk('A_100', Oe({ affixId: 100 }), 'AIwBhQ');
  return out;
})();
`);
console.log('   ', JSON.stringify(sandbox.__idcheck, null, 0));

/* ------------------------------------------------------------------ parse save */
console.log('[5] parsing save exactly like pvmImportSaveFile ...');
let raw = fs.readFileSync(SAVE, 'utf8');
if (raw.slice(0, 5) !== 'EPOCH') throw new Error('save does not start with EPOCH');
// tool: var s = JSON.parse(text.slice(5));
sandbox.__saveText = raw;
run(`
window.__save = JSON.parse(__saveText.slice(5));
window.__saveMeta = {
  characterName: __save.characterName,
  level: __save.level,
  characterClass: __save.characterClass,
  chosenMastery: __save.chosenMastery,
  savedItems: (__save.savedItems || []).length
};
`, 'parse-save.js');
console.log('    save:', JSON.stringify(sandbox.__saveMeta));

/* ------------------------------------------------------------------ decode items */
console.log('[6] pvmDecodeItem on all savedItems ...');
run(`
window.__decoded = (window.__save.savedItems || []).map(function (it, idx) {
  var d;
  try { d = pvmDecodeItem(it.data); } catch (e) { d = { __error: String(e && e.message || e) }; }
  return {
    index: idx,
    containerID: it.containerID,
    inventoryPosition: it.inventoryPosition,
    quantity: it.quantity,
    formatVersion: it.formatVersion,
    dataLength: (it.data || []).length,
    decoded: d
  };
});
window.__decodedSummary = {
  total: window.__decoded.length,
  decodedNonNull: window.__decoded.filter(function (x) { return x.decoded && !x.decoded.__error; }).length,
  nulls: window.__decoded.filter(function (x) { return !x.decoded; }).map(function (x) { return x.index; }),
  errors: window.__decoded.filter(function (x) { return x.decoded && x.decoded.__error; })
    .map(function (x) { return { index: x.index, error: x.decoded.__error }; })
};
`, 'decode-items.js');
console.log('    decoded:', JSON.stringify(sandbox.__decodedSummary));

/* ------------------------------------------------------------------ build wire */
console.log('[7] pvmBuildWireFromSave(save) ...');
run(`
window.__wire = pvmBuildWireFromSave(window.__save);
window.__wireSummary = {
  keys: Object.keys(window.__wire),
  equipment: Object.keys(window.__wire.equipment || {}),
  idols: (window.__wire.idols || []).length,
  blessings: Object.keys(window.__wire.blessings || {}),
  weaverTreeContainers: Object.keys(window.__wire.weaverTreeContainers || {}),
  charTreeSelected: Object.keys(window.__wire.charTree && window.__wire.charTree.selected || {}).length,
  skillTrees: (window.__wire.skillTrees || []).map(function (t) { return t.treeID; }),
  hud: window.__wire.hud || [],
  dataVersion: window.__wire.dataVersion,
  bio: window.__wire.bio
};
`, 'build-wire.js');
console.log('    wire summary:', JSON.stringify(sandbox.__wireSummary));

/* ------------------------------------------------------------------ write back (stretch) */
console.log('[8] pvmEncodeItem(decoded) byte round-trip ...');
run(`
window.__roundtrip = window.__decoded.map(function (x) {
  var orig = window.__save.savedItems[x.index].data;
  if (!x.decoded || x.decoded.__error || !x.decoded.id) return { index: x.index, containerID: x.containerID, encoded: null };
  var enc = null;
  try { enc = pvmEncodeItem(x.decoded); } catch (e) { enc = null; }
  var diffOffsets = [];
  if (enc) {
    var n = Math.max(orig.length, enc.length);
    for (var i = 0; i < n; i++) if (orig[i] !== enc[i]) diffOffsets.push({ offset: i, orig: orig[i] === undefined ? null : orig[i], enc: enc[i] === undefined ? null : enc[i] });
  }
  return {
    index: x.index, containerID: x.containerID,
    id: x.decoded.id,
    lengthOrig: orig.length, lengthEnc: enc ? enc.length : null,
    identical: !!(enc && enc.length === orig.length && enc.every(function (b, i) { return b === orig[i]; })),
    diffOffsets: diffOffsets,
    enc: enc
  };
});
window.__roundtripSummary = {
  total: window.__roundtrip.length,
  encoded: window.__roundtrip.filter(function (r) { return r.enc; }).length,
  identical: window.__roundtrip.filter(function (r) { return r.identical; }).length,
  notIdentical: window.__roundtrip.filter(function (r) { return r.enc && !r.identical; })
    .map(function (r) { return { index: r.index, containerID: r.containerID, id: r.id, lenOrig: r.lengthOrig, lenEnc: r.lengthEnc }; })
};
`, 'roundtrip.js');
console.log('    roundtrip:', JSON.stringify(sandbox.__roundtripSummary));

/* ------------------------------------------------------------------ outputs */
function writeJson(name, value) {
  fs.writeFileSync(path.join(OUT, name), JSON.stringify(value, null, 1) + '\n', 'utf8');
  console.log('    wrote', name, '(' + fs.statSync(path.join(OUT, name)).size + ' bytes)');
}
console.log('[9] writing outputs ...');
writeJson('wire-from-save.json', sandbox.__wire);
writeJson('decoded-items-tool-native.json', {
  source: SAVE,
  tool: TOOL,
  toolFunctions: ['pvmBuildWireFromSave', 'pvmDecodeItem', 'pvmDecodeS4', 'pvmDecodeS5',
    'pvmReadAffsGen', 'pvmEncodeItem', 'pvmBuildAffixRev'],
  saveMeta: sandbox.__saveMeta,
  summary: sandbox.__decodedSummary,
  items: sandbox.__decoded,
  roundtrip: {
    summary: sandbox.__roundtripSummary,
    items: sandbox.__roundtrip.map(function (r) { return { index: r.index, containerID: r.containerID, id: r.id, identical: r.identical }; })
  }
});
writeJson('run-summary.json', {
  tool: TOOL,
  save: SAVE,
  dbFiles: DB_JS,
  idEncoderSelfCheck: sandbox.__idcheck,
  bootstrap: sandbox.__bootstrap,
  saveMeta: sandbox.__saveMeta,
  decoded: sandbox.__decodedSummary,
  wire: sandbox.__wireSummary,
  roundtrip: sandbox.__roundtripSummary
});
// full round-trip detail incl. differed byte offsets (not the encoded arrays)
writeJson('roundtrip-report.json', {
  note: 'pvmEncodeItem(pvmDecodeItem(data)) vs original data; tool encoder hardcodes instance id [0,0,0x7f,0xff] and default forging potential, so the only expected differences are those fields',
  summary: sandbox.__roundtripSummary,
  items: sandbox.__roundtrip.map(function (r) {
    return {
      index: r.index, containerID: r.containerID, id: r.id,
      lengthOrig: r.lengthOrig, lengthEnc: r.lengthEnc, identical: r.identical,
      diffOffsets: r.diffOffsets
    };
  })
});
console.log('done.');