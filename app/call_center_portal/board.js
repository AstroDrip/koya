"use strict";

const byId = (name) => document.getElementById(name);
const date = new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata", dateStyle: "short", timeStyle: "medium",
});
let token = "";
let session = 0;
let timer;
let feed;
let refreshing;
let calls = [];

function say(message, error = false) {
    byId("message").textContent = message;
    byId("message").dataset.error = String(error);
}

function logout(message) {
    session += 1;
    token = "";
    clearTimeout(timer);
    feed?.abort();
    calls = [];
    for (const id of ["observed", "calls", "escalations", "followups"]) byId(id).replaceChildren();
    byId("board").hidden = true;
    byId("sign-out").hidden = true;
    byId("entry").hidden = false;
    byId("session").value = "";
    byId("session").focus();
    say(message);
}

function label(text) {
    return text.replaceAll("_", " ").replace(/^[a-z]/, (character) => character.toUpperCase());
}

function showRows(element, rows, empty) {
    const items = (rows.length ? rows : [empty]).map((text) => {
        const item = document.createElement("li");
        item.textContent = text;
        return item;
    });
    byId(element).replaceChildren(...items);
}

function showCalls() {
    const filter = byId("filter").value;
    const visible = calls.filter((call) => filter === "all" || call.observation === filter);
    showRows("calls", visible.map((call) => [
        "Call " + call.id, label(call.direction),
        label(call.state === "started" ? call.observation : call.state),
        date.format(call.observed_at * 1000) + " IST", call.caller_number,
        call.case_id ? "Case ID " + call.case_id : "Case not linked",
    ].filter(Boolean).join(" · ")), "No calls match this view.");
}

async function fetchBoard(part) {
    const response = await fetch("/api/call-centre/" + part, {
        headers: { Authorization: "Bearer " + token }, cache: "no-store",
    });
    if (!response.ok) {
        const error = new Error("The call board could not load. Retrying in 10 seconds.");
        error.status = response.status;
        throw error;
    }
    return response.json();
}

async function load(generation) {
    if (refreshing === generation) return;
    refreshing = generation;
    try {
        const [page, escalation, followups] = await Promise.all([
            fetchBoard("calls"), fetchBoard("escalations"), fetchBoard("followups"),
        ]);
        if (generation !== session) return;
        calls = page.items;
        showCalls();
        showRows("escalations", escalation.items.map((item) =>
            ["Call " + item.call_id, label(item.category)].join(" · ")), "No open escalations.");
        showRows("followups", followups.items.map((item) =>
            ["Case ID " + item.case_id, label(item.state), item.informed_at ? "Customer informed" : ""]
                .filter(Boolean).join(" · ")), "No Happy Calls recorded.");
        byId("observed").textContent = "Board refreshed " + date.format(page.as_of * 1000) + " IST";
        byId("board").hidden = false;
        byId("entry").hidden = true;
        byId("sign-out").hidden = false;
        say("");
    } catch (error) {
        if (generation !== session) return;
        if (error.status === 401 || error.status === 403) {
            logout("Owner access required. Enter a valid session token.");
            return;
        }
        say("The call board could not refresh. Displayed states may be stale. Retrying in 10 seconds.", true);
    } finally {
        if (refreshing === generation) refreshing = undefined;
    }
    if (generation === session) timer = setTimeout(() => load(generation), 10000);
}

async function followEvents(generation, signal) {
    let cursor;
    while (generation === session && !signal.aborted) {
        try {
            const headers = { Authorization: "Bearer " + token };
            if (cursor !== undefined) headers["Last-Event-ID"] = cursor;
            const response = await fetch("/api/operations/events", { headers, signal, cache: "no-store" });
            if (response.status === 401 || response.status === 403) {
                if (generation === session) logout("Owner access required. Enter a valid session token.");
                return;
            }
            if (!response.ok || !response.body) throw new Error("Event feed unavailable.");
            const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
            let buffer = "";
            try {
                for (;;) {
                    const part = await reader.read();
                    if (part.done || generation !== session) break;
                    buffer += part.value;
                    let boundary;
                    while ((boundary = buffer.indexOf("\n\n")) >= 0) {
                        const frame = buffer.slice(0, boundary);
                        buffer = buffer.slice(boundary + 2);
                        const id = /^id: (\d+)$/m.exec(frame);
                        if (id) cursor = id[1];
                        if (refreshing !== generation && (/^event: reset$/m.test(frame) ||
                            (/^event: change$/m.test(frame) && /"topic":"(call|followup|escalation)"/.test(frame)))) {
                            clearTimeout(timer);
                            load(generation);
                        }
                    }
                }
            } finally {
                reader.releaseLock();
            }
        } catch {
            if (signal.aborted) return;
        }
        await new Promise((resolve) => setTimeout(resolve, 1000));
    }
}

byId("sign-in").addEventListener("submit", (event) => {
    event.preventDefault();
    clearTimeout(timer);
    feed?.abort();
    session += 1;
    token = byId("session").value.trim();
    byId("session").value = "";
    say("Loading…");
    load(session);
    feed = new AbortController();
    followEvents(session, feed.signal);
});
byId("sign-out").addEventListener("click", () => logout("Signed out of the board."));
byId("filter").addEventListener("change", showCalls);
