// Smoke test for a built review-graph page, run by `review_graph.py smoke` (needs node).
// Runs the page's scripts with browser-like globals (a non-configurable `top`, a stub DOM) in three modes:
//   none  - no window.claude: must render the snapshot and say so
//   live  - fake db/user/comments: must go live, recolor on a verdict, and send feedback + a batch to Claude
//   throw - claude.use rejects: must show the error and keep the snapshot
const fs = require("fs"), vm = require("vm");
const [, , pagePath] = process.argv;
const page = fs.readFileSync(pagePath, "utf8");
// Tokenize <script> elements left to right, like an HTML parser: a literal "<script>" inside a JSON block (a PR title,
// say) is text, because the block's own opening tag has already consumed it. "</" is escaped in the JSON by the builder.
const blocks = [...page.matchAll(/<script([^>]*)>([\s\S]*?)<\/script>/g)].map(m => ({ attrs: m[1], body: m[2] }));
const scripts = blocks.filter(b => !b.attrs.trim()).map(b => b.body);
const jsonTag = id => (blocks.find(b => b.attrs.includes(`id="${id}"`)) || {}).body;
if (!scripts.length || !jsonTag("snapshot") || !jsonTag("pageconfig")) {
  console.log(`FAIL: ${pagePath} is not a built review-graph page (no page script, or no embedded snapshot/config)`);
  process.exit(1);
}
const fail = msg => { console.log("FAIL: " + msg); process.exitCode = 1; };

function run(mode) {
  return new Promise(resolve => {
    const listeners = {}, calls = { sent: [], updates: [], adds: [] };
    const el = id => ({
      id, hidden: false, className: "", value: "", checked: true, disabled: false, dataset: {},
      textContent: id === "snapshot" ? jsonTag("snapshot") : id === "pageconfig" ? jsonTag("pageconfig") : "",
      _html: "", style: { setProperty() {} },
      get innerHTML() { return this._html }, set innerHTML(v) { this._html = v },
      setAttribute(k, v) { this[k] = v }, removeAttribute(k) { delete this[k] },
      addEventListener(t, f) { (listeners[id + ":" + t] ??= []).push(f) },
      querySelectorAll(sel) {
        if (sel === ".row" || sel === "g.node") return [...this._html.matchAll(/data-id="([^"]+)"/g)].map(m => {
          const r = { dataset: { id: m[1] }, addEventListener(t, f) { (listeners["row:" + m[1] + ":" + t] ??= []).push(f) } }; return r;
        });
        return [];
      },
      scrollIntoView() {},
    });
    const els = {};
    const ctx = {
      console, setTimeout, clearTimeout, setInterval: () => 0, Promise, JSON, Date, Math, Object, String, Number, Array, RegExp, Error,
      document: { getElementById: id => (els[id] ??= el(id)), addEventListener: () => {}, hidden: false },
      matchMedia: () => ({ matches: false }), location: { hostname: "" },
      addEventListener: (t, f) => { (listeners["window:" + t] ??= []).push(f) },
    };
    ctx.window = ctx;
    Object.defineProperty(ctx, "top", { get() { return ctx }, configurable: false });
    if (mode === "live" || mode === "sendfail") {
      const snap = JSON.parse(jsonTag("snapshot"));
      const open = n => n.kind === "pr" && !["merged", "closed"].includes(n.status);
      // Prefer an open PR; when everything is merged, use any PR (the recolor check is then skipped).
      const first = snap.nodes.find(n => open(n) && n.review_step) || snap.nodes.find(n => n.kind === "pr");
      const second = snap.nodes.find(n => open(n) && n.review_step && n.id !== first.id && n.patch_id);
      const data = { nodes: snap.nodes, edges: snap.edges,
        reviews: [{ node_id: first.id, verdict: "approved", head: first.head },
                  ...(second ? [{ node_id: second.id, verdict: "approved", head: "00000000", patch_id: "not-the-same" }] : [])], feedback: [] };
      ctx.__second = second; ctx.__open = open;
      const qs = rows => ({ docs: rows.map((r, i) => ({ id: r.id || r.node_id || "f" + i, exists: true, data: () => r })), size: rows.length, empty: !rows.length });
      const subs = {};
      const db = {
        collection: c => ({
          onSnapshot: (next) => { subs[c] = next; setTimeout(() => next(qs(data[c] || [])), 1); return () => {} },
          add: async d => { calls.adds.push(d); data.feedback.push({ ...d, id: "fb1" }); setTimeout(() => subs.feedback && subs.feedback(qs(data.feedback)), 1); return { id: "fb1" } },
          doc: id => ({ id: id || "fb1", update: async () => {},
            set: async d => { if (c === "feedback") { calls.adds.push(d); data.feedback.push({ ...d, id: id || "fb1" }); setTimeout(() => subs.feedback && subs.feedback(qs(data.feedback)), 1) } } }),
        }),
        doc: p => ({ set: async () => {}, update: async d => { calls.updates.push([p, d]) } }),
      };
      const user = { id: async () => "u_test", can: async () => true };
      // canSendToClaude says "no_session" on purpose: the controls must not be gated on it.
      const comments = { canSendToClaude: async () => "no_session", anchorFor: async () => ({ path: "x", x: 0, y: 0 }),
        sendToClaude: mode === "sendfail"
          ? async () => { throw { code: "claude_unavailable", message: "test" } }
          : async t => { calls.sent.push(t.text); return { threadId: "t1", commentId: "c1" } } };
      ctx.claude = { use: async n => ({ db, user, comments })[n] || null };
      ctx.__first = first;
    }
    if (mode === "throw") ctx.claude = { use: async () => { throw { code: "not_granted", message: "test" } } };
    vm.createContext(ctx);
    try { for (const s of scripts) vm.runInContext(s, ctx, { filename: pagePath }); }
    catch (e) { fail(`${mode}: script threw ${e.name}: ${e.message}`); return resolve(); }
    setTimeout(async () => {
      const status = els.status.textContent, svg = els.g._html;
      if (!svg.includes('class="node"')) fail(`${mode}: graph rendered no boxes`);
      if ("hidden" in els.g) fail(`${mode}: the SVG still has its hidden attribute`);
      if (!els.queue._html.includes('class="row"')) fail(`${mode}: queue is empty`);
      console.log(`${mode}: status "${status}"`);
      if (mode === "none" && !/snapshot/i.test(status)) fail("none: status should mention the snapshot");
      if (mode === "throw" && !/not_granted/.test(status)) fail("throw: status should show the error code");
      if (mode === "sendfail") {
        const id = ctx.__first.id, $ = ctx.document.getElementById;
        (listeners[`row:${id}:click`] || [])[0]?.();
        $("fbtext").value = "will fail"; $("fbsend").checked = true;
        await (listeners["sendfb:click"] || [])[0]?.();
        await new Promise(r => setTimeout(r, 20));
        if (!calls.adds.length) fail("sendfail: the feedback row must still be saved when the send fails");
        if (!/Not sent/.test($("msg").textContent) || !/claude_unavailable/.test($("msg").textContent)) fail(`sendfail: the failure must show next to Save feedback (got "${$("msg").textContent}")`);
        if ($("sendall").disabled) fail("sendfail: the batch button must not be gated on canSendToClaude");
        await (listeners["sendall:click"] || [])[0]?.();
        await new Promise(r => setTimeout(r, 20));
        if (!/Not sent/.test($("sendhint").textContent)) fail(`sendfail: the batch failure must show next to the button (got "${$("sendhint").textContent}")`);
        console.log(`sendfail: errors shown: "${$("msg").textContent}" / "${$("sendhint").textContent}"`);
      }
      if (mode === "live") {
        if (!/^Live/.test(status)) fail("live: status should say Live");
        const id = ctx.__first.id, re = new RegExp(`data-id="${id}"[\\s\\S]{0,500}approved-b`);
        if (ctx.__open(ctx.__first) && !re.test(svg)) fail("live: a verdict at the current head should recolor its box");
        if (ctx.__second) {
          const re2 = new RegExp(`data-id="${ctx.__second.id}"[\\s\\S]{0,1200}?re-check`);
          if (!re2.test(svg)) fail("live: an approval at an older head with a changed patch-id should show re-check");
        }
        // Save feedback with "Send to Claude now" checked (open the panel through the queue row first).
        (listeners[`row:${id}:click`] || [])[0]?.();
        const $ = ctx.document.getElementById;
        if (!/id="fbtext"/.test(els.panel?._html || "")) fail("live: clicking a queue row did not open the feedback panel");
        $("fbtext").value = "please split this file";
        $("fbsend").checked = true;
        await (listeners["sendfb:click"] || [])[0]?.();
        await new Promise(r => setTimeout(r, 20));
        if (!calls.adds.length) fail("live: Save feedback wrote no feedback row");
        if (!calls.sent.some(t => t.includes("please split this file"))) fail("live: checked feedback was not sent to Claude");
        if (!calls.updates.some(([p, d]) => p === "feedback/fb1" && d.sent_at)) fail("live: sent feedback was not stamped sent_at");
        // The batch button sends the unsent verdict.
        await (listeners["sendall:click"] || [])[0]?.();
        if (!calls.sent.some(t => t.includes("verdict"))) fail("live: the batch button did not send the verdict");
        console.log(`live: ${calls.sent.length} comment(s) sent to Claude, ${calls.updates.length} row(s) stamped`);
      }
      resolve();
    }, 60);
  });
}

(async () => {
  for (const m of ["none", "throw", "live", "sendfail"]) await run(m);
  console.log(process.exitCode ? "smoke FAILED" : "smoke OK");
  process.exit(process.exitCode || 0); // the page's own timers (a 15 s status reset) would keep node alive otherwise
})();
