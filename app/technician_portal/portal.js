"use strict";

const byId = (id) => document.getElementById(id);
const entry = byId("entry");
const workspace = byId("workspace");
const detail = byId("detail");
let token = "";
let generation = 0;

function say(message, error = false) {
    byId("message").textContent = message;
    byId("message").dataset.error = String(error);
}

function label(value) {
    return value.replaceAll("_", " ").replace(/^[a-z]/, (first) => first.toUpperCase());
}

function node(tag, text = "") {
    const element = document.createElement(tag);
    element.textContent = text;
    return element;
}

function logout() {
    generation += 1;
    token = "";
    entry.hidden = false;
    workspace.hidden = true;
    byId("sign-out").hidden = true;
    byId("identity").textContent = "";
    byId("session").value = "";
    byId("job-list").replaceChildren();
    byId("notifications").replaceChildren();
    detail.replaceChildren();
    detail.hidden = true;
}

async function api(path, body) {
    const started = generation;
    const options = { headers: { Authorization: "Bearer " + token }, cache: "no-store" };
    if (body !== undefined) {
        options.method = "POST";
        options.headers["Content-Type"] = "application/json";
        options.body = JSON.stringify(body);
    }
    const response = await fetch("/api/technician" + path, options);
    if (started !== generation) throw new Error("Session ended.");
    if (response.status === 401) {
        logout();
        throw new Error("Session expired or invalid. Ask dispatch for a new token.");
    }
    if (response.status === 404) {
        throw new Error("This job or notification is no longer available. Refresh your list.");
    }
    if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(typeof payload.detail === "string" ? payload.detail : "Action failed. Try again.");
    }
    const result = response.status === 204 ? null : await response.json();
    if (started !== generation) throw new Error("Session ended.");
    return result;
}

async function run(button, task) {
    button.disabled = true;
    say("Loading…");
    try {
        await task();
        say("");
    } catch (error) {
        say(error.message, true);
    } finally {
        button.disabled = false;
    }
}

function action(text, handler) {
    const button = node("button", text);
    button.type = "button";
    button.addEventListener("click", () => run(button, handler));
    return button;
}

async function refresh() {
    const [jobs, notifications] = await Promise.all([api("/jobs"), api("/notifications")]);
    const rows = jobs.items.map((job) => {
        const row = node("li");
        row.append(node("span", [job.reference, label(job.status), job.pincode].join(" · ")),
            action("View job", () => openJob(job.id)));
        return row;
    });
    byId("job-list").replaceChildren(...(rows.length ? rows : [node("li", "No active jobs.")]));
    const alerts = notifications.items.map((notification) => {
        const row = node("li");
        row.append(node("span", label(notification.kind)), action("Mark read", async () => {
            await api("/notifications/" + notification.id + "/acknowledge", {});
            await refresh();
        }));
        return row;
    });
    byId("notifications").replaceChildren(...(alerts.length ? alerts : [node("li", "No unread notifications.")]));
}

const transitions = {
    offered: ["accepted", "declined"], accepted: ["en_route"], en_route: ["in_progress"],
    in_progress: ["awaiting_parts", "completed"], awaiting_parts: ["in_progress"],
};

function formFor(title, choices, field, submit) {
    const form = node("form");
    const select = node("select");
    const prompt = node("label", title);
    prompt.htmlFor = field + "-option";
    select.id = prompt.htmlFor;
    for (const choice of choices) {
        const option = node("option", label(choice));
        option.value = choice;
        select.append(option);
    }
    const text = node("textarea");
    text.id = field + "-text";
    text.required = true;
    text.maxLength = 800;
    const notesLabel = node("label", "Details");
    notesLabel.htmlFor = text.id;
    const button = node("button", "Save");
    form.append(prompt, select, notesLabel, text, button);
    form.addEventListener("submit", (event) => {
        event.preventDefault();
        run(button, () => submit(select.value, text.value));
    });
    return form;
}

const visitDate = new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata", dateStyle: "medium", timeStyle: "short",
});

async function openJob(id) {
    const path = "/jobs/" + id;
    const job = await api(path);
    detail.replaceChildren(node("h2", "Job " + job.reference),
        node("p", [label(job.status), job.pincode, job.skill].join(" · ")));
    for (const field of ["description", "product_name", "model_number", "serial_number"]) {
        if (job[field]) detail.append(node("p", label(field) + ": " + job[field]));
    }
    if (job.appointment) {
        const { starts_at: start, ends_at: end } = job.appointment;
        detail.append(node("p", visitDate.format(start * 1000) + " – " + visitDate.format(end * 1000) + " IST"));
        const contact = Object.values(job.visit_contact ?? {}).filter(Boolean);
        if (contact.length) detail.append(node("p", contact.join(" · ")));
    } else if (job.status !== "offered") {
        detail.append(node("p", "Appointment pending."));
    }
    const next = transitions[job.status]?.filter((state) => state !== "en_route" || job.appointment) ?? [];
    if (next.length) {
        detail.append(formFor("Change job status", next, "transition", async (status, reason) => {
            await api(path + "/transitions", { status, reason, version: job.version });
            detail.hidden = true;
            detail.replaceChildren();
            await refresh();
            if (!["declined", "completed"].includes(status)) await openJob(id);
        }));
    }
    if (job.status !== "offered") {
        for (const note of job.notes ?? []) detail.append(node("p", label(note.kind) + ": " + note.body));
        detail.append(formFor("Add a job entry", ["note", "inspection", "parts", "escalation"], "note", async (kind, text) => {
            await api(path + "/notes", { kind, text });
            await openJob(id);
        }));
        const estimate = node("section");
        estimate.setAttribute("aria-label", "Service estimate");
        estimate.append(action("Prepare estimate", () => openEstimate(path, estimate)));
        detail.append(estimate);
    }
    detail.hidden = false;
    detail.focus();
}

function estimateInput(type, name, attributes = {}) {
    const field = node("input");
    field.type = type;
    field.name = name;
    for (const [key, value] of Object.entries(attributes)) field.setAttribute(key, value);
    return field;
}

function fieldLabel(text, control) {
    const wrapper = node("label", text);
    wrapper.append(control);
    return wrapper;
}

function rupees(paise) {
    const exact = BigInt(paise);
    return "₹" + (exact / 100n).toLocaleString("en-IN") + "." + String(exact % 100n).padStart(2, "0");
}

async function openEstimate(path, target) {
    const catalog = await api(path + "/estimate-catalog");
    if (catalog.items.length === 0) {
        target.replaceChildren(node("p", "No approved prices are available for this job."));
        return;
    }
    const form = node("form");
    const rows = node("div");
    const expiry = estimateInput("date", "expiry", { required: "" });
    const notes = estimateInput("text", "notes", { maxlength: "800" });
    const exclusions = estimateInput("text", "exclusions", { maxlength: "800" });
    const save = node("button", "Submit for review");
    const key = crypto.randomUUID();

    function addLine() {
        const fields = node("fieldset");
        fields.append(node("legend", "Estimate line"));
        const price = node("select");
        price.name = "price";
        for (const option of catalog.items) {
            const choice = node("option", option.label + " · " + rupees(option.unit_paise));
            choice.value = option.code;
            price.append(choice);
        }
        const quantity = estimateInput("number", "quantity", { min: "1", max: "1000", value: "1", required: "" });
        const discount = estimateInput("number", "discount", { min: "0", max: "1000000000", value: "0", required: "" });
        const remove = node("button", "Remove line");
        remove.type = "button";
        remove.addEventListener("click", () => {
            if (rows.children.length > 1) fields.remove();
        });
        fields.append(fieldLabel("Approved item", price), fieldLabel("Quantity", quantity),
            fieldLabel("Discount in paise", discount), remove);
        rows.append(fields);
    }

    const add = node("button", "Add line");
    add.type = "button";
    add.addEventListener("click", () => {
        if (rows.children.length < 100) addLine();
    });
    addLine();
    form.append(rows, add, fieldLabel("Valid through (Kolkata date)", expiry),
        fieldLabel("Notes", notes), fieldLabel("Exclusions", exclusions), save);
    form.addEventListener("submit", (event) => {
        event.preventDefault();
        run(save, async () => {
            const lines = Array.from(rows.children, (fields) => {
                const code = fields.querySelector("[name=price]").value;
                const item = catalog.items.find((price) => price.code === code);
                return {
                    price_code: code, price_revision: item.revision,
                    quantity: fields.querySelector("[name=quantity]").valueAsNumber,
                    discount_paise: fields.querySelector("[name=discount]").valueAsNumber,
                };
            });
            const quote = await api(path + "/estimates", {
                operation_key: key, previous_id: catalog.current_estimate_id,
                notes: notes.value.trim() || null, exclusions: exclusions.value.trim() || null,
                expires_at: expiry.value + "T23:59:59+05:30", lines,
            });
            target.replaceChildren(node("h3", "Estimate submitted"),
                node("p", "Revision " + quote.revision + " · " + rupees(quote.total_paise) + " · " + label(quote.status)));
        });
    });
    target.replaceChildren(node("h3", catalog.current_estimate_id ? "Revise estimate" : "Prepare estimate"));
    if (catalog.minimum_inspection_paise !== null) {
        target.append(node("p", "Configured inspection minimum " + rupees(catalog.minimum_inspection_paise)));
    }
    target.append(form);
}

byId("sign-in").addEventListener("submit", (event) => {
    event.preventDefault();
    const button = event.currentTarget.querySelector("button");
    run(button, async () => {
        generation += 1;
        token = byId("session").value.trim();
        byId("session").value = "";
        const identity = await api("/session");
        byId("identity").textContent = identity.name + " · " + identity.code;
        entry.hidden = true;
        workspace.hidden = false;
        byId("sign-out").hidden = false;
        await refresh();
    });
});
byId("sign-out").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
        await api("/session/end", {});
        say("Signed out.");
    } catch {
        say("This device is signed out. Dispatch can revoke the token if needed.", true);
    } finally {
        logout();
        button.disabled = false;
        byId("session").focus();
    }
});
byId("reload").addEventListener("click", (event) => run(event.currentTarget, async () => {
    detail.hidden = true;
    detail.replaceChildren();
    await refresh();
}));
