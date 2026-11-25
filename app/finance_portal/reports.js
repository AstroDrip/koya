"use strict";

const get = (id) => document.getElementById(id);
let token = "";
let session = 0;
let reportQuery = "";

function displayMoney(value) {
    if (!Number.isSafeInteger(value)) return "See CSV for exact amount";
    const magnitude = BigInt(Math.abs(value));
    return (value < 0 ? "−" : "") + "₹" + (magnitude / 100n).toLocaleString("en-IN")
        + "." + String(magnitude % 100n).padStart(2, "0");
}

function say(text, error = false) {
    get("message").textContent = text;
    get("message").dataset.error = String(error);
}

function renderList(id, values, empty) {
    const rows = (values.length ? values : [empty]).map((text) => {
        const row = document.createElement("li");
        row.textContent = text;
        return row;
    });
    get(id).replaceChildren(...rows);
}

function logout(message) {
    session += 1;
    token = "";
    reportQuery = "";
    for (const id of ["period", "totals", "trends", "invoices"]) get(id).replaceChildren();
    get("export").disabled = true;
    get("workspace").hidden = true;
    get("sign-out").hidden = true;
    get("entry").hidden = false;
    get("session").value = "";
    get("session").focus();
    say(message);
}

async function request(path, csv = false) {
    const started = session;
    const response = await fetch("/api/finance/" + path, {
        headers: { Authorization: "Bearer " + token }, cache: "no-store",
    });
    if (started !== session) throw new Error("Session ended.");
    if (response.status === 401 || response.status === 403) {
        logout("Finance access required. Enter a valid session token.");
        throw new Error("Finance access required.");
    }
    if (!response.ok) {
        const problem = await response.json().catch(() => ({}));
        throw new Error(problem.detail ?? "Unable to load finance information.");
    }
    const result = csv ? await response.blob() : await response.json();
    if (started !== session) throw new Error("Session ended.");
    return result;
}

async function run(button, action) {
    const started = session;
    button.disabled = true;
    say("Loading…");
    try {
        await action();
        if (started === session) say("");
    } catch (error) {
        if (started === session) say(error.message, true);
    } finally {
        button.disabled = false;
    }
}

function kolkataDate() {
    const parts = new Intl.DateTimeFormat("en-CA", {
        timeZone: "Asia/Kolkata", year: "numeric", month: "2-digit", day: "2-digit",
    }).formatToParts(new Date());
    const fields = Object.fromEntries(parts.map((part) => [part.type, part.value]));
    return [fields.year, fields.month, fields.day].join("-");
}

async function report() {
    const query = new URLSearchParams({ start: get("start").value, end: get("end").value, group: get("group").value });
    const result = await request("reports/sales?" + query);
    reportQuery = query.toString();
    get("period").textContent = result.start + " to " + result.end + " · Kolkata business dates";
    const labels = {
        gross_billed_paise: "Gross billed", discounts_paise: "Discounts", net_sales_paise: "Net sales before tax",
        net_tax_paise: "Net tax", payments_paise: "Payments collected", refunds_paise: "Refunds",
        outstanding_paise: "Outstanding at period end", refund_due_paise: "Refunds due at period end",
    };
    renderList("totals", Object.entries(labels).map(([key, label]) =>
        label + " · " + displayMoney(result.totals[key])), "No ledger activity.");
    renderList("trends", result.trends.map((entry) => [
        entry.period, "Net sales " + displayMoney(entry.net_sales_paise),
        "Net collected " + displayMoney(entry.net_collected_paise),
    ].join(" · ")), "No ledger activity.");
    get("export").disabled = false;
}

get("start").value = kolkataDate();
get("end").value = get("start").value;
get("sign-in").addEventListener("submit", (event) => {
    event.preventDefault();
    session += 1;
    token = get("session").value.trim();
    get("session").value = "";
    run(event.currentTarget.querySelector("button"), async () => {
        const invoices = await request("invoices");
        await report();
        renderList("invoices", invoices.items.map((invoice) => [
            invoice.number, displayMoney(invoice.total_paise), invoice.status.replaceAll("_", " "),
        ].join(" · ")), "No invoices issued.");
        get("workspace").hidden = false;
        get("entry").hidden = true;
        get("sign-out").hidden = false;
    });
});
get("filters").addEventListener("submit", (event) => {
    event.preventDefault();
    run(event.currentTarget.querySelector("button"), report);
});
get("export").addEventListener("click", (event) => run(event.currentTarget, async () => {
    const content = await request("reports/sales.csv?" + reportQuery, true);
    const url = URL.createObjectURL(content);
    const link = document.createElement("a");
    link.href = url;
    link.download = "mrcare-sales.csv";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
}));
get("sign-out").addEventListener("click", () => logout("Signed out of the finance view."));
