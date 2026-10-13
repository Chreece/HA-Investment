const indicationsUrl = new URL(import.meta.url);
const ASSET_REVISION = String(indicationsUrl.searchParams.get("asset_revision") || "").trim();
const RUNTIME_REVISION = String(indicationsUrl.searchParams.get("runtime_revision") || "").trim();
if (!ASSET_REVISION) throw new Error("HA Investment panel revision is missing");

const contributionUrl = new URL("./investment-panel-contribution.js", indicationsUrl);
contributionUrl.searchParams.set("asset_revision", ASSET_REVISION);
if (RUNTIME_REVISION) contributionUrl.searchParams.set("runtime_revision", RUNTIME_REVISION);
await import(contributionUrl.href);

const PANEL_NAME = `investment-panel-${ASSET_REVISION.split("-").at(-1)}`;
const Panel = customElements.get(PANEL_NAME);
if (!Panel) throw new Error("HA Investment contribution panel failed to register");

const MINIMUM_LOT_COPY = {
  en:{title:"Affordable whole-unit adjustment",body:"The budget did not reject these candidates. Their proportional target was below one whole unit, so the smallest affordable unit was used after checking the selected risk, diversification, cash-reserve and cost limits again."},
  de:{title:"Anpassung auf eine bezahlbare ganze Einheit",body:"Das Budget hat diese Kandidaten nicht abgelehnt. Ihr proportionaler Zielbetrag lag unter einer ganzen Einheit; deshalb wurde nach erneuter Prüfung von Risiko, Diversifikation, Barreserve und Kosten die kleinste bezahlbare Einheit verwendet."},
  el:{title:"Προσαρμογή σε προσιτή ακέραιη μονάδα",body:"Το ποσό δεν απέρριψε αυτούς τους υποψηφίους. Ο αναλογικός στόχος τους ήταν μικρότερος από μία ακέραιη μονάδα, οπότε χρησιμοποιήθηκε η μικρότερη προσιτή μονάδα αφού ελέγχθηκαν ξανά τα όρια κινδύνου, διαφοροποίησης, αποθεματικού και κόστους."},
};
function minimumLotLanguage(panel){
  const raw=String(panel?._lang||panel?._haLang||panel?.hass?.language||"en").toLowerCase();
  const base=raw.split("-")[0];
  return MINIMUM_LOT_COPY[base]?base:"en";
}
function minimumLotEscape(value){
  return String(value??"").replace(/[&<>"']/g,ch=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[ch]));
}

const proto = Panel.prototype;
if (!proto.__investmentIndicationsPurchaseBudgetOnly) {
  Object.defineProperty(proto, "__investmentIndicationsPurchaseBudgetOnly", {value: true});

  const planningPayload = proto.indicationPlanningPayload;
  proto.indicationPlanningPayload = function(...args) {
    // The control is deliberately absent from Indications. A stale saved value
    // must neither fail client-side validation nor enter the request payload.
    return {...planningPayload.apply(this, args), existing_cash: 0};
  };

  const allocationExplanation = proto.indicationAllocationExplanation;
  proto.indicationAllocationExplanation = function(result) {
    const existing = allocationExplanation.call(this, result) || "";
    const meta = result?.allocation?.minimum_lot_override;
    if (!meta || Number(meta.used || 0) <= 0) return existing;
    const copy = MINIMUM_LOT_COPY[minimumLotLanguage(this)] || MINIMUM_LOT_COPY.en;
    const symbols = (Array.isArray(meta.selected) ? meta.selected : [])
      .map(item=>String(item?.symbol||item?.provider_id||""))
      .filter(Boolean)
      .join(", ");
    return `<div class="allocation-explanation cash-explanation minimum-lot-explanation"><strong>${minimumLotEscape(copy.title)}</strong><div><span>${minimumLotEscape(copy.body)}${symbols?`<small>${minimumLotEscape(symbols)}</small>`:""}</span></div></div>${existing}`;
  };
}
