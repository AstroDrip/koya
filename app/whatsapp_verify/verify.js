"use strict";

const element = (id) => document.getElementById(id);
let pending = null;

function message(text, error = false) {
    element("message").textContent = text;
    element("message").dataset.error = String(error);
}

async function send(action, body) {
    const response = await fetch("/api/whatsapp/verification/" + action, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body), cache: "no-store",
    });
    if (!response.ok) {
        if (response.status === 503) throw new Error("Verification is currently unavailable. Contact MR CARE support.");
        if (response.status === 429) throw new Error("Too many requests. Try again later.");
        throw new Error("Verification failed. Check your details or start again.");
    }
    return response.json();
}

function bind(form, operation) {
    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const button = form.querySelector("button");
        button.disabled = true;
        element("restart").disabled = true;
        message("Checking…");
        try {
            await operation();
        } catch (error) {
            message(error.message, true);
        } finally {
            button.disabled = false;
            element("restart").disabled = false;
        }
    });
}

bind(element("start"), async () => {
    const reference = element("reference").value.trim();
    const token = element("token").value.trim();
    element("token").value = "";
    const result = await send("start", { reference, token, mobile: element("mobile").value.trim() });
    pending = { reference, token, challenge_id: result.challenge_id };
    element("start").hidden = true;
    element("confirm").hidden = false;
    element("restart").hidden = false;
    element("code").focus();
    message("Enter the code delivered to your verified contact.");
});

bind(element("confirm"), async () => {
    const code = element("code").value;
    element("code").value = "";
    await send("confirm", { ...pending, code });
    pending = null;
    element("confirm").hidden = true;
    element("restart").hidden = true;
    message("Case verified. Return to WhatsApp and send STATUS to check your case.");
});

element("restart").addEventListener("click", () => {
    pending = null;
    element("start").reset();
    element("confirm").reset();
    element("confirm").hidden = true;
    element("restart").hidden = true;
    element("start").hidden = false;
    element("mobile").focus();
    message("");
});
