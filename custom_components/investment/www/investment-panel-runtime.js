import "./investment-panel.js?v=0.4.0-r25";

await customElements.whenDefined("investment-panel");
const Panel = customElements.get("investment-panel");

const TEXT = {
  en:{portfolioContext:"Portfolio context",usePortfolio:"Use my portfolio",ignorePortfolio:"Ignore my portfolio",portfolioContextHint:"Use current holdings, concentration and overlap when ranking. Ignore mode ranks only from the selected market/risk inputs.",newOnly:"New instruments only",newOnlyHint:"Remove instruments you currently hold from the candidate set while still using the rest of your portfolio as context."},
  de:{portfolioContext:"Portfoliokontext",usePortfolio:"Mein Portfolio berücksichtigen",ignorePortfolio:"Mein Portfolio ignorieren",portfolioContextHint:"Berücksichtigt bestehende Positionen, Konzentration und Überschneidungen. Im Ignorieren-Modus zählt nur die ausgewählte Markt-/Risikobewertung.",newOnly:"Nur neue Instrumente",newOnlyHint:"Blendet Instrumente aus, die du aktuell hältst, berücksichtigt aber den Rest deines Portfolios weiterhin als Kontext."},
  el:{portfolioContext:"Πλαίσιο χαρτοφυλακίου",usePortfolio:"Χρήση του χαρτοφυλακίου μου",ignorePortfolio:"Αγνόηση του χαρτοφυλακίου μου",portfolioContextHint:"Λαμβάνει υπόψη τις υπάρχουσες θέσεις, τη συγκέντρωση και την επικάλυψη. Η αγνόηση κατατάσσει μόνο με βάση τα επιλεγμένα δεδομένα αγοράς/κινδύνου.",newOnly:"Μόνο νέα μέσα",newOnlyHint:"Αφαιρεί από τους υποψηφίους όσα μέσα κατέχεις ήδη, αλλά συνεχίζει να χρησιμοποιεί το υπόλοιπο χαρτοφυλάκιο ως πλαίσιο."},
  fr:{portfolioContext:"Contexte du portefeuille",usePortfolio:"Utiliser mon portefeuille",ignorePortfolio:"Ignorer mon portefeuille",portfolioContextHint:"Tient compte des positions, de la concentration et du chevauchement actuels. Le mode Ignorer classe uniquement selon les données marché/risque sélectionnées.",newOnly:"Nouveaux instruments uniquement",newOnlyHint:"Retire les instruments actuellement détenus tout en conservant le reste du portefeuille comme contexte."},
  es:{portfolioContext:"Contexto de cartera",usePortfolio:"Usar mi cartera",ignorePortfolio:"Ignorar mi cartera",portfolioContextHint:"Usa posiciones actuales, concentración y solapamiento. Ignorar clasifica solo con los datos de mercado/riesgo seleccionados.",newOnly:"Solo instrumentos nuevos",newOnlyHint:"Elimina los instrumentos que ya tienes, pero sigue usando el resto de la cartera como contexto."},
  it:{portfolioContext:"Contesto portafoglio",usePortfolio:"Usa il mio portafoglio",ignorePortfolio:"Ignora il mio portafoglio",portfolioContextHint:"Considera posizioni, concentrazione e sovrapposizione correnti. Ignora classifica solo dai dati mercato/rischio selezionati.",newOnly:"Solo strumenti nuovi",newOnlyHint:"Rimuove gli strumenti già detenuti mantenendo il resto del portafoglio come contesto."},
  pt:{portfolioContext:"Contexto da carteira",usePortfolio:"Usar a minha carteira",ignorePortfolio:"Ignorar a minha carteira",portfolioContextHint:"Considera posições atuais, concentração e sobreposição. Ignorar classifica apenas pelos dados de mercado/risco selecionados.",newOnly:"Apenas instrumentos novos",newOnlyHint:"Remove os instrumentos já detidos, mantendo o restante da carteira como contexto."},
  nl:{portfolioContext:"Portefeuillecontext",usePortfolio:"Mijn portefeuille gebruiken",ignorePortfolio:"Mijn portefeuille negeren",portfolioContextHint:"Gebruikt huidige posities, concentratie en overlap. Negeren rangschikt alleen op de gekozen markt-/risicodata.",newOnly:"Alleen nieuwe instrumenten",newOnlyHint:"Verwijdert instrumenten die je al bezit, terwijl de rest van de portefeuille als context blijft gelden."},
  pl:{portfolioContext:"Kontekst portfela",usePortfolio:"Uwzględnij mój portfel",ignorePortfolio:"Ignoruj mój portfel",portfolioContextHint:"Uwzględnia bieżące pozycje, koncentrację i nakładanie. Tryb ignorowania ocenia tylko na podstawie wybranych danych rynkowych/ryzyka.",newOnly:"Tylko nowe instrumenty",newOnlyHint:"Usuwa instrumenty już posiadane, ale nadal używa pozostałej części portfela jako kontekstu."},
  tr:{portfolioContext:"Portföy bağlamı",usePortfolio:"Portföyümü kullan",ignorePortfolio:"Portföyümü yok say",portfolioContextHint:"Mevcut pozisyonları, yoğunlaşmayı ve örtüşmeyi hesaba katar. Yok say modu yalnızca seçilen piyasa/risk verilerini kullanır.",newOnly:"Yalnızca yeni araçlar",newOnlyHint:"Zaten sahip olduğunuz araçları çıkarır, ancak portföyün geri kalanını bağlam olarak kullanmaya devam eder."},
  ru:{portfolioContext:"Контекст портфеля",usePortfolio:"Учитывать мой портфель",ignorePortfolio:"Игнорировать мой портфель",portfolioContextHint:"Учитывает текущие позиции, концентрацию и пересечения. Режим игнорирования ранжирует только по выбранным рыночным/рисковым данным.",newOnly:"Только новые инструменты",newOnlyHint:"Исключает уже имеющиеся инструменты, сохраняя остальной портфель как контекст."},
  uk:{portfolioContext:"Контекст портфеля",usePortfolio:"Враховувати мій портфель",ignorePortfolio:"Ігнорувати мій портфель",portfolioContextHint:"Враховує поточні позиції, концентрацію та перетини. Режим ігнорування ранжує лише за вибраними ринковими/ризиковими даними.",newOnly:"Лише нові інструменти",newOnlyHint:"Виключає вже наявні інструменти, але зберігає решту портфеля як контекст."},
  cs:{portfolioContext:"Kontext portfolia",usePortfolio:"Použít mé portfolio",ignorePortfolio:"Ignorovat mé portfolio",portfolioContextHint:"Zohlední aktuální pozice, koncentraci a překryv. Režim ignorování řadí pouze podle zvolených tržních/rizikových dat.",newOnly:"Pouze nové nástroje",newOnlyHint:"Vyřadí nástroje, které již držíte, ale zbytek portfolia nadále používá jako kontext."},
  zh:{portfolioContext:"投资组合上下文",usePortfolio:"使用我的投资组合",ignorePortfolio:"忽略我的投资组合",portfolioContextHint:"考虑当前持仓、集中度和重叠。忽略模式仅根据所选市场/风险数据排序。",newOnly:"仅新工具",newOnlyHint:"移除当前已持有的工具，同时仍使用其余投资组合作为上下文。"},
  ja:{portfolioContext:"ポートフォリオの考慮",usePortfolio:"現在のポートフォリオを使用",ignorePortfolio:"現在のポートフォリオを無視",portfolioContextHint:"現在の保有、集中度、重複を考慮します。無視モードでは選択した市場/リスクデータだけで順位付けします。",newOnly:"新しい商品だけ",newOnlyHint:"現在保有している商品を候補から除外し、その他のポートフォリオ情報は文脈として利用します。"}
};

function escHtml(value){return String(value??"").replace(/[&<>"']/g,ch=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"})[ch]);}
function langFor(panel){
  const preferred=String(panel?._preferredLang||"").toLowerCase();
  const raw=(preferred&&preferred!=="auto")?preferred:String(panel?._lang||panel?._haLang||"en").toLowerCase();
  const base=raw.split("-")[0];
  return TEXT[raw]?raw:(TEXT[base]?base:"en");
}
function labels(panel){return TEXT[langFor(panel)]||TEXT.en;}

if(Panel&&!Panel.prototype.__portfolioContextRuntime){
  const proto=Panel.prototype;
  Object.defineProperty(proto,"__portfolioContextRuntime",{value:true});

  const draftFromStored=proto.indicationDraftFromStored;
  proto.indicationDraftFromStored=function(raw){
    const draft=draftFromStored.call(this,raw);
    const p=raw||{};
    draft.portfolioContext=["use","ignore"].includes(String(p.portfolio_context||""))?String(p.portfolio_context):"use";
    draft.newOnly=String(p.existing_instruments||"allow")==="exclude";
    return draft;
  };

  const capture=proto.captureIndicationDraft;
  proto.captureIndicationDraft=function(form){
    capture.call(this,form);
    if(!this._indicationDraft||!form)return;
    const scope=String(this._indicationDraft.scope||"discover");
    const context=String(form.querySelector('[name="indication_portfolio_context"]')?.value||"use");
    const newOnly=!!form.querySelector('[name="indication_new_only"]')?.checked;
    this._indicationDraft.portfolioContext=scope==="portfolio"?"use":(["use","ignore"].includes(context)?context:"use");
    this._indicationDraft.newOnly=scope==="portfolio"?false:newOnly;
  };

  const preferencesPayload=proto.indicationPreferencesPayload;
  proto.indicationPreferencesPayload=function(draft=this._indicationDraft){
    const payload=preferencesPayload.call(this,draft);
    const d=draft||{};
    payload.portfolio_context=["use","ignore"].includes(String(d.portfolioContext||""))?String(d.portfolioContext):"use";
    payload.existing_instruments=d.newOnly?"exclude":"allow";
    return payload;
  };

  const baseCall=proto.call;
  proto.call=function(message){
    if(message&&message.type==="investment/indication"){
      const draft=this._indicationDraft||{};
      message={...message,portfolio_context:["use","ignore"].includes(String(draft.portfolioContext||""))?String(draft.portfolioContext):"use",existing_instruments:draft.newOnly?"exclude":"allow"};
    }
    return baseCall.call(this,message);
  };

  const modalHtml=proto.indicationModalHtml;
  proto.indicationModalHtml=function(){
    let html=modalHtml.call(this);
    const draft=this._indicationDraft||{};
    const t=labels(this);
    const scope=String(draft.scope||"discover");
    const context=scope==="portfolio"?"use":(["use","ignore"].includes(String(draft.portfolioContext||""))?String(draft.portfolioContext):"use");
    const disabled=scope==="portfolio"?"disabled":"";
    const controls=`<label class="setting-field" title="${escHtml(t.portfolioContextHint)}"><span>${escHtml(t.portfolioContext)}</span><select name="indication_portfolio_context" ${disabled}><option value="use" ${context==="use"?"selected":""}>${escHtml(t.usePortfolio)}</option><option value="ignore" ${context==="ignore"?"selected":""}>${escHtml(t.ignorePortfolio)}</option></select></label><label class="setting-field whole-unit-query" title="${escHtml(t.newOnlyHint)}"><span>${escHtml(t.newOnly)}</span><input name="indication_new_only" type="checkbox" ${draft.newOnly&&scope!=="portfolio"?"checked":""} ${disabled}/></label>`;
    const start=html.indexOf('<details class="indication-advanced"');
    const end=start>=0?html.indexOf("</div></details>",start):-1;
    if(end>=0)html=html.slice(0,end)+controls+html.slice(end);
    return html;
  };
}
