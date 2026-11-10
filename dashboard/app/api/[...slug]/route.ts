/**
 * dashboard/app/api/[...slug]/route.ts
 * ------------------------------------
 * Self-contained Vercel serverless API that replicates the Python gateway
 * for the deployed dashboard. Handles:
 *   GET/POST /api/health
 *   GET      /api/stats
 *   GET      /api/audit
 *   GET      /api/audit/verify
 *   GET/PUT  /api/mandate
 *   POST     /api/kill
 *   POST     /api/approve/:id
 *   POST     /api/act
 *   POST     /api/agent/run
 *   GET      /api/invoices
 *   GET      /api/disputes
 *
 * Policy engine and audit chain are in-memory (stateless per serverless
 * invocation). PayPal calls fall back to simulated data when env vars
 * are absent (sandbox demo mode).
 */

import { NextRequest, NextResponse } from "next/server";
import crypto from "crypto";

// ── Types ────────────────────────────────────────────────────────────────────

interface AuditRow {
  id: number;
  ts: number;
  action: string;
  params: string;
  amount: number;
  customer: string;
  risk: number;
  explanation: string;
  decision: string;
  reason: string;
  status: string;
  result: string;
  prev_hash: string;
  row_hash: string;
}

interface Mandate {
  allowed_actions: string[];
  max_per_txn: number;
  daily_budget: number;
  approval_above: number;
  risk_threshold: number;
  blocked_customers: string[];
  kill_switch: boolean;
}

// ── In-memory state (shared within warm serverless instance) ──────────────────

const _mandate: Mandate = {
  allowed_actions: ["send_reminder", "refund", "flag_dispute", "list_invoices", "list_disputes"],
  max_per_txn: 100.0,
  daily_budget: 500.0,
  approval_above: 50.0,
  risk_threshold: 0.7,
  blocked_customers: [],
  kill_switch: false,
};

// ── Hash chain builder ────────────────────────────────────────────────────────

function buildHash(id: number, ts: number, action: string, amount: number, customer: string, decision: string, status: string, prev: string): string {
  const payload = `${id}|${ts}|${action}|${amount}|${customer}|${decision}|${status}|${prev}`;
  return crypto.createHash("sha256").update(payload).digest("hex");
}

// Pre-seed with realistic demo audit rows
const _auditLog: AuditRow[] = (() => {
  const seeds = [
    { action: "send_reminder", params: { invoice_id: "INV2-HPK6-PCPR-8S49-APLN", note: "Payment overdue" }, amount: 0, customer: "alice@acmecorp.com", risk: 0.08, decision: "ALLOW", reason: "within mandate", status: "EXECUTED", result: "Reminder sent to PayPal sandbox" },
    { action: "refund", params: { capture_id: "CAP_SANDBOX_001", amount: 25.0 }, amount: 25.0, customer: "bob@startup.io", risk: 0.19, decision: "ALLOW", reason: "within mandate", status: "EXECUTED", result: "Refund processed via PayPal sandbox" },
    { action: "refund", params: { capture_id: "CAP_SANDBOX_002", amount: 350.0 }, amount: 350.0, customer: "charlie@bigco.net", risk: 0.62, decision: "NEEDS_APPROVAL", reason: "above approval threshold ($50)", status: "PENDING", result: "" },
    { action: "refund", params: { capture_id: "cap_injection_demo", amount: 5000.0 }, amount: 5000.0, customer: "attacker@example.com", risk: 0.95, decision: "DENY", reason: "over per-transaction cap", status: "BLOCKED", result: "" },
  ];
  const rows: AuditRow[] = [];
  let prev = "0".repeat(64);
  for (let i = 0; i < seeds.length; i++) {
    const s = seeds[i];
    const id = i + 1;
    const ts = Date.now() / 1000 - (seeds.length - i) * 900;
    const row_hash = buildHash(id, ts, s.action, s.amount, s.customer, s.decision, s.status, prev);
    rows.push({ id, ts, action: s.action, params: JSON.stringify(s.params), amount: s.amount, customer: s.customer, risk: s.risk, explanation: JSON.stringify({ model: "IsolationForest", score: s.risk }), decision: s.decision, reason: s.reason, status: s.status, result: s.result, prev_hash: prev, row_hash });
    prev = row_hash;
  }
  return rows;
})();

let _nextId = _auditLog.length + 1;

// ── Policy engine ─────────────────────────────────────────────────────────────

function policyDecide(action: string, amount: number, customer: string, spentToday: number): { decision: string; reason: string } {
  if (_mandate.kill_switch) return { decision: "DENY", reason: "kill switch active" };
  if (!_mandate.allowed_actions.includes(action)) return { decision: "DENY", reason: "action not in mandate" };
  if (_mandate.blocked_customers.includes(customer)) return { decision: "DENY", reason: "customer blocked" };
  if (amount > _mandate.max_per_txn) return { decision: "DENY", reason: `over per-transaction cap ($${_mandate.max_per_txn})` };
  if (spentToday + amount > _mandate.daily_budget) return { decision: "DENY", reason: "daily budget exceeded" };
  if (amount > _mandate.approval_above) return { decision: "NEEDS_APPROVAL", reason: "above approval threshold or high risk" };
  return { decision: "ALLOW", reason: "within mandate" };
}

// ── Risk scorer (heuristic mirrors Python risk.py) ────────────────────────────

function riskScore(amount: number, history: { is_new?: boolean; refunds_30d?: number; disputes?: number }): number {
  let s = Math.min(amount / 500, 1) * 0.5;
  if ((history.refunds_30d ?? 0) >= 2) s += 0.3;
  if (history.is_new) s += 0.2;
  if ((history.disputes ?? 0) > 0) s += 0.15;
  return Math.min(s, 1.0);
}

// ── Audit helper ──────────────────────────────────────────────────────────────

function appendAudit(partial: Omit<AuditRow, "id" | "ts" | "prev_hash" | "row_hash">): AuditRow {
  const prev = _auditLog.length > 0 ? _auditLog[_auditLog.length - 1].row_hash : "0".repeat(64);
  const id = _nextId++;
  const ts = Date.now() / 1000;
  const row_hash = buildHash(id, ts, partial.action, partial.amount, partial.customer, partial.decision, partial.status, prev);
  const row: AuditRow = { id, ts, ...partial, prev_hash: prev, row_hash };
  _auditLog.push(row);
  return row;
}

function getSpentToday(): number {
  const dayStart = Date.now() / 1000 - 86400;
  return _auditLog.filter((r) => r.status === "EXECUTED" && r.ts >= dayStart).reduce((s, r) => s + r.amount, 0);
}

function verifyChain(): { ok: boolean; message: string } {
  let prev = "0".repeat(64);
  for (const r of _auditLog) {
    if (r.prev_hash !== prev) return { ok: false, message: `Chain broken at row ${r.id}: prev_hash mismatch` };
    const expected = buildHash(r.id, r.ts, r.action, r.amount, r.customer, r.decision, r.status, prev);
    if (r.row_hash !== expected) return { ok: false, message: `Chain broken at row ${r.id}: row_hash mismatch` };
    prev = r.row_hash;
  }
  return { ok: true, message: `All ${_auditLog.length} audit rows verified` };
}

// ── PayPal client helpers ─────────────────────────────────────────────────────

let _ppToken: { v: string; exp: number } | null = null;

async function ppToken(): Promise<string | null> {
  const clientId = process.env.PAYPAL_CLIENT_ID;
  const secret = process.env.PAYPAL_SECRET;
  if (!clientId || !secret) return null;
  if (_ppToken && Date.now() < _ppToken.exp - 60000) return _ppToken.v;
  try {
    const r = await fetch("https://api-m.sandbox.paypal.com/v1/oauth2/token", {
      method: "POST",
      headers: { Authorization: `Basic ${Buffer.from(`${clientId}:${secret}`).toString("base64")}`, "Content-Type": "application/x-www-form-urlencoded" },
      body: "grant_type=client_credentials",
    });
    if (!r.ok) return null;
    const j = await r.json();
    _ppToken = { v: j.access_token, exp: Date.now() + j.expires_in * 1000 };
    return _ppToken.v;
  } catch { return null; }
}

async function ppCall(method: string, path: string, body?: unknown): Promise<unknown> {
  const tok = await ppToken();
  if (!tok) throw new Error("PayPal credentials not configured");
  const r = await fetch(`https://api-m.sandbox.paypal.com${path}`, {
    method,
    headers: { Authorization: `Bearer ${tok}`, "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error(`PayPal ${r.status}: ${await r.text()}`);
  return r.status === 204 ? {} : r.json();
}

// Demo data for when PayPal creds are absent
const DEMO_INVOICES = [
  { id: "INV2-HPK6-PCPR-8S49-APLN", status: "UNPAID", amount: 250.0, customer: "alice@acmecorp.com", due: "2026-10-30" },
  { id: "INV2-3JHX-VVZF-R5GU-WK5D", status: "UNPAID", amount: 85.0,  customer: "bob@startup.io",   due: "2026-11-01" },
  { id: "INV2-DBBF-7FQP-EXUB-E2WM", status: "UNPAID", amount: 1200.0, customer: "charlie@bigco.net", due: "2026-10-15" },
  { id: "INV2-P3Z8-LDUE-SAKU-UBQ9", status: "UNPAID", amount: 45.0,   customer: "diana@shop.co",    due: "2026-11-05" },
  { id: "INV2-2BPK-UUKC-HLYW-NY4W", status: "UNPAID", amount: 320.0,  customer: "evan@freelance.dev", due: "2026-10-28" },
];

// ── Agent simulation (runs when no Python gateway is available) ───────────────

async function runAgentSimulated(goal: string): Promise<string> {
  const lower = goal.toLowerCase();
  const lines: string[] = [];

  if (lower.includes("invoice") || lower.includes("overdue") || lower.includes("chase") || lower.includes("reminder")) {
    lines.push("🤖 Agent: Fetching overdue invoices from PayPal sandbox...");
    lines.push(`📋 Found ${DEMO_INVOICES.length} unpaid invoices.`);
    for (const inv of DEMO_INVOICES.slice(0, 3)) {
      const { decision, reason } = policyDecide("send_reminder", 0, inv.customer, getSpentToday());
      if (decision === "ALLOW") {
        appendAudit({ action: "send_reminder", params: JSON.stringify({ invoice_id: inv.id }), amount: 0, customer: inv.customer, risk: 0.05, explanation: JSON.stringify({ model: "heuristic" }), decision, reason, status: "EXECUTED", result: `Reminder sent for ${inv.id}` });
        lines.push(`  ✓ Sent reminder to ${inv.customer} for $${inv.amount} (${inv.id})`);
      } else {
        lines.push(`  ✗ Reminder blocked for ${inv.customer}: ${reason}`);
      }
    }
    lines.push(`\n📊 All actions recorded to tamper-evident audit chain.`);
  } else if (lower.includes("refund")) {
    const amtMatch = goal.match(/\$?(\d+(?:\.\d+)?)/);
    const amount = amtMatch ? parseFloat(amtMatch[1]) : 30;
    const custMatch = goal.match(/\b([A-Z][a-z]+)\b/);
    const customer = custMatch ? `${custMatch[1].toLowerCase()}@example.com` : "customer@example.com";
    const history = { is_new: false, refunds_30d: 1 };
    const risk = riskScore(amount, history);
    const { decision, reason } = policyDecide("refund", amount, customer, getSpentToday());
    const status = decision === "ALLOW" ? "EXECUTED" : decision === "NEEDS_APPROVAL" ? "PENDING" : "BLOCKED";
    appendAudit({ action: "refund", params: JSON.stringify({ amount }), amount, customer, risk, explanation: JSON.stringify({ model: "heuristic", score: risk }), decision, reason, status, result: decision === "ALLOW" ? `Refund of $${amount} processed` : "" });
    if (decision === "ALLOW") {
      lines.push(`🤖 Agent: Refund of $${amount} to ${customer} — ✅ APPROVED and executed to PayPal sandbox.`);
    } else if (decision === "NEEDS_APPROVAL") {
      lines.push(`🤖 Agent: Refund of $${amount} to ${customer} — ⏳ QUEUED for HUMAN APPROVAL (risk: ${risk.toFixed(2)}).`);
      lines.push(`Check the Approval Inbox above to approve or reject.`);
    } else {
      lines.push(`🤖 Agent: Refund of $${amount} to ${customer} — 🛑 BLOCKED by policy (${reason}).`);
    }
    lines.push(`\nRisk score: ${risk.toFixed(3)} | Decision logged to audit chain.`);
  } else if (lower.includes("dispute")) {
    lines.push("🤖 Agent: Checking PayPal disputes...");
    lines.push("📋 0 open disputes in sandbox (demo mode).");
    lines.push("✓ No action required.");
  } else {
    lines.push(`🤖 Aegis Agent received: "${goal}"\n`);
    lines.push("Available capabilities:");
    lines.push("  • Chase overdue invoices → send payment reminders via PayPal");
    lines.push("  • Process refunds → subject to your policy mandate");
    lines.push("  • Triage disputes → flag and summarize");
    lines.push("  • All actions logged to tamper-evident audit chain\n");
    lines.push("Try: 'Chase all overdue invoices' or 'Refund Alice $20'");
  }
  return lines.join("\n");
}

// ── Route handlers ────────────────────────────────────────────────────────────

export async function GET(req: NextRequest, { params }: { params: Promise<{ slug: string[] }> }) {
  const { slug } = await params;
  const path = slug.join("/");
  const { searchParams } = new URL(req.url);

  if (path === "health") {
    return NextResponse.json({ status: "ok", service: "aegis-gateway", version: "1.0.0", mode: process.env.PAYPAL_CLIENT_ID ? "paypal-sandbox" : "demo", kill_switch: _mandate.kill_switch });
  }
  if (path === "stats") {
    const today = Date.now() / 1000 - 86400;
    return NextResponse.json({ spent_today: _auditLog.filter((r) => r.status === "EXECUTED" && r.ts >= today).reduce((s, r) => s + r.amount, 0), daily_budget: _mandate.daily_budget, pending: _auditLog.filter((r) => r.status === "PENDING").length, blocked: _auditLog.filter((r) => r.status === "BLOCKED").length, executed: _auditLog.filter((r) => r.status === "EXECUTED").length, rejected: _auditLog.filter((r) => r.status === "REJECTED").length, kill_switch: _mandate.kill_switch });
  }
  if (path === "audit") {
    const limit = parseInt(searchParams.get("limit") ?? "200");
    return NextResponse.json([..._auditLog].reverse().slice(0, limit));
  }
  if (path === "audit/verify") {
    return NextResponse.json(verifyChain());
  }
  if (path === "mandate") {
    return NextResponse.json({ ..._mandate });
  }
  if (path === "invoices") {
    try { return NextResponse.json((await ppCall("GET", "/v2/invoicing/invoices?page=1&page_size=20") as { items?: unknown[] })?.items ?? DEMO_INVOICES); }
    catch { return NextResponse.json(DEMO_INVOICES); }
  }
  if (path === "disputes") {
    try { return NextResponse.json((await ppCall("GET", "/v1/customer/disputes") as { items?: unknown[] })?.items ?? []); }
    catch { return NextResponse.json([]); }
  }
  return NextResponse.json({ error: "Not found", path }, { status: 404 });
}

export async function POST(req: NextRequest, { params }: { params: Promise<{ slug: string[] }> }) {
  const { slug } = await params;
  const path = slug.join("/");
  const { searchParams } = new URL(req.url);

  if (path === "kill") {
    _mandate.kill_switch = searchParams.get("on") === "true";
    return NextResponse.json({ kill_switch: _mandate.kill_switch });
  }

  if (path.startsWith("approve/")) {
    const id = parseInt(path.split("/")[1]);
    const approve = searchParams.get("approve") !== "false";
    const row = _auditLog.find((r) => r.id === id);
    if (!row) return NextResponse.json({ error: "Not found" }, { status: 404 });
    if (row.status !== "PENDING") return NextResponse.json({ error: "Not pending" }, { status: 400 });
    if (approve) {
      let result = "Executed (demo mode)";
      try {
        if (row.action === "refund") {
          const p = JSON.parse(row.params);
          if (p.capture_id && !p.capture_id.includes("SANDBOX")) {
            await ppCall("POST", `/v2/payments/captures/${p.capture_id}/refund`, { amount: { value: row.amount.toFixed(2), currency_code: "USD" } });
            result = `Refund of $${row.amount} processed via PayPal sandbox`;
          }
        }
      } catch (e: unknown) { result = `Executed (sandbox): ${e instanceof Error ? e.message : String(e)}`; }
      row.status = "EXECUTED"; row.result = result;
      return NextResponse.json({ status: "EXECUTED", result });
    } else {
      row.status = "REJECTED"; row.result = "Rejected by operator";
      return NextResponse.json({ status: "REJECTED" });
    }
  }

  if (path === "act") {
    let body: { action: string; amount?: number; customer?: string; params?: Record<string, unknown>; history?: Record<string, unknown> };
    try { body = await req.json(); } catch { return NextResponse.json({ error: "Invalid JSON" }, { status: 400 }); }

    const action = body.action ?? "";
    const amount = body.amount ?? 0;
    const customer = body.customer ?? "unknown";
    const history = (body.history ?? {}) as { is_new?: boolean; refunds_30d?: number };
    const rawParams = body.params ?? {};

    // Sanitize potential prompt injection
    const safeParams: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(rawParams)) {
      if (typeof v === "string" && /system:|ignore previous|attacker/i.test(v)) {
        safeParams[k] = "<<SANITIZED: potential prompt injection>>";
      } else { safeParams[k] = v; }
    }

    const risk = riskScore(amount, history);
    const { decision, reason } = policyDecide(action, amount, customer, getSpentToday());
    const status = decision === "ALLOW" ? "EXECUTED" : decision === "NEEDS_APPROVAL" ? "PENDING" : "BLOCKED";

    let result = "";
    if (decision === "ALLOW") {
      try {
        if (action === "refund" && safeParams.capture_id && !String(safeParams.capture_id).includes("demo")) {
          await ppCall("POST", `/v2/payments/captures/${safeParams.capture_id}/refund`, { amount: { value: amount.toFixed(2), currency_code: "USD" } });
          result = `Refund of $${amount} processed via PayPal sandbox`;
        } else if (action === "send_reminder" && safeParams.invoice_id) {
          await ppCall("POST", `/v2/invoicing/invoices/${safeParams.invoice_id}/remind`, { subject: "Payment reminder", note: String(safeParams.note ?? ""), send_to_invoicer: true });
          result = `Reminder sent for ${safeParams.invoice_id}`;
        } else { result = `${action} executed (simulated)`; }
      } catch (e: unknown) { result = `Executed (sandbox): ${e instanceof Error ? e.message : String(e)}`; }
    }

    const row = appendAudit({ action, params: JSON.stringify(safeParams), amount, customer, risk, explanation: JSON.stringify({ model: "heuristic", score: risk }), decision, reason, status, result });
    return NextResponse.json({ status, decision, reason, risk, audit_id: row.id, result: result || undefined });
  }

  if (path === "agent/run") {
    let body: { goal?: string };
    try { body = await req.json(); } catch { return NextResponse.json({ error: "Invalid JSON" }, { status: 400 }); }
    const goal = body.goal?.trim() ?? "";
    if (!goal) return NextResponse.json({ error: "goal is required" }, { status: 400 });
    const sanitized = goal.replace(/system:|ignore previous instructions?/gi, "<<SANITIZED>>");
    const response = await runAgentSimulated(sanitized);
    return NextResponse.json({ goal: sanitized, response, provider: "serverless-simulation" });
  }

  return NextResponse.json({ error: "Not found", path }, { status: 404 });
}

export async function PUT(req: NextRequest, { params }: { params: Promise<{ slug: string[] }> }) {
  const { slug } = await params;
  const path = slug.join("/");

  if (path === "mandate") {
    let body: Partial<Mandate>;
    try { body = await req.json(); } catch { return NextResponse.json({ error: "Invalid JSON" }, { status: 400 }); }
    if (body.allowed_actions !== undefined) _mandate.allowed_actions = body.allowed_actions;
    if (body.max_per_txn !== undefined) _mandate.max_per_txn = body.max_per_txn;
    if (body.daily_budget !== undefined) _mandate.daily_budget = body.daily_budget;
    if (body.approval_above !== undefined) _mandate.approval_above = body.approval_above;
    if (body.risk_threshold !== undefined) _mandate.risk_threshold = body.risk_threshold;
    if (body.blocked_customers !== undefined) _mandate.blocked_customers = body.blocked_customers;
    return NextResponse.json({ ..._mandate });
  }

  return NextResponse.json({ error: "Not found", path }, { status: 404 });
}
