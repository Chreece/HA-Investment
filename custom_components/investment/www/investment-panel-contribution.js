const policyUrl = new URL(import.meta.url);
const ASSET_REVISION = String(policyUrl.searchParams.get("asset_revision") || "").trim();
const RUNTIME_REVISION = String(policyUrl.searchParams.get("runtime_revision") || "").trim();
if (!ASSET_REVISION) throw new Error("HA Investment panel revision is missing");

const runtimeUrl = new URL("./investment-panel-runtime.js", policyUrl);
runtimeUrl.searchParams.set("asset_revision", ASSET_REVISION);
if (RUNTIME_REVISION) runtimeUrl.searchParams.set("runtime_revision", RUNTIME_REVISION);
await import(runtimeUrl.href);

const PANEL_NAME = `investment-panel-${ASSET_REVISION.split("-").at(-1)}`;
const Panel = customElements.get(PANEL_NAME);
if (!Panel) throw new Error("HA Investment Indications runtime failed to register");

const COPY = {
  en:{label:"Owned-investment filter",exclude:"Exclude investments I already own",include:"Include investments I already own",current:"Analyse my current holdings",hint:"Existing holdings are used only for exact identity filtering. Their past performance, volatility, concentration and drawdown do not change the new-buy calculation.",step:"Applies the practical rules to the new purchase budget only: data quality, costs, cash reserve, diversification, per-candidate limits and whole-unit requirements."},
  de:{label:"Filter für bereits gehaltene Anlagen",exclude:"Bereits gehaltene Anlagen ausschließen",include:"Bereits gehaltene Anlagen einbeziehen",current:"Meine aktuellen Positionen analysieren",hint:"Bestehende Positionen werden nur zur exakten Identitätsfilterung verwendet. Ihre bisherige Wertentwicklung, Volatilität, Konzentration und ihr Drawdown verändern die Berechnung des neuen Kaufs nicht.",step:"Wendet die praktischen Regeln nur auf das neue Kaufbudget an: Datenqualität, Kosten, Barreserve, Diversifikation, Kandidatenlimits und Vorgaben für ganze Stücke."},
  el:{label:"Φίλτρο επενδύσεων που ήδη κατέχω",exclude:"Αφαίρεση επενδύσεων που ήδη κατέχω",include:"Να εμφανίζονται και επενδύσεις που ήδη κατέχω",current:"Ανάλυση των τωρινών θέσεών μου",hint:"Οι υπάρχουσες θέσεις χρησιμοποιούνται μόνο για ακριβή αναγνώριση και αφαίρεση ίδιων επενδύσεων. Η παλιά απόδοση, η μεταβλητότητα, η συγκέντρωση και η πτώση τους δεν αλλάζουν τον υπολογισμό της νέας αγοράς.",step:"Εφαρμόζει τους πρακτικούς κανόνες μόνο στο νέο ποσό αγοράς: ποιότητα δεδομένων, κόστη, αποθεματικό μετρητών, διαφοροποίηση, όρια ανά υποψήφιο και ακέραιες μονάδες."},
  fr:{label:"Filtre des placements déjà détenus",exclude:"Exclure les placements que je détiens déjà",include:"Inclure les placements que je détiens déjà",current:"Analyser mes positions actuelles",hint:"Les positions existantes servent uniquement à identifier exactement les placements déjà détenus. Leur performance, volatilité, concentration et baisse passées ne modifient pas le calcul du nouvel achat.",step:"Applique les règles pratiques uniquement au nouveau budget d'achat : qualité des données, coûts, réserve de liquidités, diversification, limites par candidat et unités entières."},
  es:{label:"Filtro de inversiones ya poseídas",exclude:"Excluir inversiones que ya poseo",include:"Incluir inversiones que ya poseo",current:"Analizar mis posiciones actuales",hint:"Las posiciones existentes solo se usan para identificar y filtrar instrumentos exactos. Su rendimiento, volatilidad, concentración y caída anteriores no cambian el cálculo de la nueva compra.",step:"Aplica las reglas prácticas solo al nuevo presupuesto de compra: calidad de datos, costes, reserva de efectivo, diversificación, límites por candidato y unidades enteras."},
  it:{label:"Filtro degli investimenti già posseduti",exclude:"Escludi gli investimenti che possiedo già",include:"Includi gli investimenti che possiedo già",current:"Analizza le mie posizioni attuali",hint:"Le posizioni esistenti sono usate solo per identificare e filtrare esattamente gli strumenti già posseduti. Rendimento, volatilità, concentrazione e drawdown passati non cambiano il calcolo del nuovo acquisto.",step:"Applica le regole pratiche solo al nuovo budget di acquisto: qualità dei dati, costi, riserva di liquidità, diversificazione, limiti per candidato e unità intere."},
  pt:{label:"Filtro de investimentos já detidos",exclude:"Excluir investimentos que já possuo",include:"Incluir investimentos que já possuo",current:"Analisar as minhas posições atuais",hint:"As posições existentes servem apenas para identificar e filtrar instrumentos exatos. O desempenho, a volatilidade, a concentração e a queda anteriores não alteram o cálculo da nova compra.",step:"Aplica as regras práticas apenas ao novo orçamento de compra: qualidade dos dados, custos, reserva de caixa, diversificação, limites por candidato e unidades inteiras."},
  nl:{label:"Filter voor reeds aangehouden beleggingen",exclude:"Beleggingen uitsluiten die ik al bezit",include:"Beleggingen opnemen die ik al bezit",current:"Mijn huidige posities analyseren",hint:"Bestaande posities worden alleen gebruikt voor exacte identificatie en filtering. Hun eerdere rendement, volatiliteit, concentratie en drawdown veranderen de berekening van de nieuwe aankoop niet.",step:"Past de praktische regels alleen toe op het nieuwe aankoopbudget: datakwaliteit, kosten, kasreserve, spreiding, limieten per kandidaat en hele eenheden."},
  pl:{label:"Filtr już posiadanych inwestycji",exclude:"Wyklucz inwestycje, które już posiadam",include:"Uwzględnij inwestycje, które już posiadam",current:"Analizuj moje obecne pozycje",hint:"Istniejące pozycje służą wyłącznie do dokładnej identyfikacji i filtrowania. Ich wcześniejsze wyniki, zmienność, koncentracja i obsunięcie nie zmieniają obliczeń nowego zakupu.",step:"Stosuje praktyczne reguły tylko do nowego budżetu zakupu: jakość danych, koszty, rezerwa gotówki, dywersyfikacja, limity kandydatów i pełne jednostki."},
  tr:{label:"Zaten sahip olunan yatırım filtresi",exclude:"Zaten sahip olduğum yatırımları hariç tut",include:"Zaten sahip olduğum yatırımları dahil et",current:"Mevcut pozisyonlarımı analiz et",hint:"Mevcut pozisyonlar yalnızca tam kimlik eşleştirmesi ve filtreleme için kullanılır. Geçmiş performansları, oynaklıkları, yoğunlaşmaları ve düşüşleri yeni alım hesabını değiştirmez.",step:"Pratik kuralları yalnızca yeni alım bütçesine uygular: veri kalitesi, maliyetler, nakit rezervi, çeşitlendirme, aday limitleri ve tam birimler."},
  ru:{label:"Фильтр уже имеющихся инвестиций",exclude:"Исключить инвестиции, которыми я уже владею",include:"Включить инвестиции, которыми я уже владею",current:"Анализировать мои текущие позиции",hint:"Существующие позиции используются только для точной идентификации и фильтрации. Их прошлая доходность, волатильность, концентрация и просадка не меняют расчет новой покупки.",step:"Применяет практические правила только к новому бюджету покупки: качество данных, расходы, денежный резерв, диверсификация, лимиты на кандидата и целые единицы."},
  uk:{label:"Фільтр уже наявних інвестицій",exclude:"Виключити інвестиції, які я вже маю",include:"Включити інвестиції, які я вже маю",current:"Аналізувати мої поточні позиції",hint:"Наявні позиції використовуються лише для точної ідентифікації та фільтрації. Їхня минула дохідність, волатильність, концентрація й просадка не змінюють розрахунок нової покупки.",step:"Застосовує практичні правила лише до нового бюджету покупки: якість даних, витрати, грошовий резерв, диверсифікація, ліміти на кандидата та цілі одиниці."},
  cs:{label:"Filtr již držených investic",exclude:"Vyloučit investice, které již vlastním",include:"Zahrnout investice, které již vlastním",current:"Analyzovat mé současné pozice",hint:"Stávající pozice se používají pouze k přesné identifikaci a filtrování. Jejich minulá výkonnost, volatilita, koncentrace a propad nemění výpočet nového nákupu.",step:"Použije praktická pravidla pouze na nový nákupní rozpočet: kvalitu dat, náklady, hotovostní rezervu, diverzifikaci, limity kandidátů a celé jednotky."},
  hu:{label:"Már meglévő befektetések szűrője",exclude:"A már meglévő befektetéseim kizárása",include:"A már meglévő befektetéseim megjelenítése",current:"Jelenlegi pozícióim elemzése",hint:"A meglévő pozíciók csak pontos azonosításra és szűrésre szolgálnak. Korábbi teljesítményük, volatilitásuk, koncentrációjuk és visszaesésük nem módosítja az új vétel számítását.",step:"A gyakorlati szabályokat csak az új vásárlási keretre alkalmazza: adatminőség, költségek, készpénztartalék, diverzifikáció, jelöltenkénti limitek és egész egységek."},
  ro:{label:"Filtru pentru investițiile deja deținute",exclude:"Exclude investițiile pe care le dețin deja",include:"Include investițiile pe care le dețin deja",current:"Analizează pozițiile mele curente",hint:"Pozițiile existente sunt folosite numai pentru identificare și filtrare exactă. Performanța, volatilitatea, concentrarea și scăderea lor anterioară nu modifică calculul noii achiziții.",step:"Aplică regulile practice numai noului buget de cumpărare: calitatea datelor, costuri, rezervă de numerar, diversificare, limite per candidat și unități întregi."},
  sv:{label:"Filter för redan ägda investeringar",exclude:"Uteslut investeringar som jag redan äger",include:"Inkludera investeringar som jag redan äger",current:"Analysera mina nuvarande innehav",hint:"Befintliga innehav används endast för exakt identifiering och filtrering. Deras tidigare avkastning, volatilitet, koncentration och nedgång påverkar inte beräkningen av det nya köpet.",step:"Tillämpar de praktiska reglerna endast på den nya köpbudgeten: datakvalitet, kostnader, kontantreserv, diversifiering, kandidatgränser och hela enheter."},
  da:{label:"Filter for allerede ejede investeringer",exclude:"Udeluk investeringer, jeg allerede ejer",include:"Medtag investeringer, jeg allerede ejer",current:"Analysér mine nuværende positioner",hint:"Eksisterende positioner bruges kun til præcis identifikation og filtrering. Deres tidligere afkast, volatilitet, koncentration og fald ændrer ikke beregningen af det nye køb.",step:"Anvender de praktiske regler kun på det nye købsbudget: datakvalitet, omkostninger, kontantreserve, diversificering, kandidatgrænser og hele enheder."},
  fi:{label:"Jo omistettujen sijoitusten suodatin",exclude:"Sulje pois jo omistamani sijoitukset",include:"Näytä myös jo omistamani sijoitukset",current:"Analysoi nykyiset sijoitukseni",hint:"Nykyisiä omistuksia käytetään vain täsmälliseen tunnistamiseen ja suodatukseen. Niiden aiempi tuotto, volatiliteetti, keskittyminen ja arvonlasku eivät muuta uuden oston laskentaa.",step:"Soveltaa käytännön sääntöjä vain uuteen ostobudjettiin: datan laatuun, kuluihin, käteisvaraan, hajautukseen, ehdokasrajoihin ja kokonaisiin yksiköihin."},
  no:{label:"Filter for investeringer du allerede eier",exclude:"Utelat investeringer jeg allerede eier",include:"Ta med investeringer jeg allerede eier",current:"Analyser mine nåværende posisjoner",hint:"Eksisterende posisjoner brukes bare til nøyaktig identifikasjon og filtrering. Tidligere avkastning, volatilitet, konsentrasjon og fall påvirker ikke beregningen av det nye kjøpet.",step:"Bruker de praktiske reglene bare på det nye kjøpsbudsjettet: datakvalitet, kostnader, kontantreserve, diversifisering, kandidatgrenser og hele enheter."},
  ja:{label:"保有済み投資のフィルター",exclude:"すでに保有している投資を除外",include:"すでに保有している投資も含める",current:"現在の保有銘柄を分析",hint:"既存の保有は同一銘柄の正確な識別と除外にのみ使用されます。過去の成績、変動性、集中度、下落率は新規購入の計算に影響しません。",step:"実用的な条件は新規購入予算だけに適用します。データ品質、コスト、現金準備、分散、候補ごとの上限、整数単位を確認します。"},
  ko:{label:"이미 보유한 투자 필터",exclude:"이미 보유한 투자는 제외",include:"이미 보유한 투자도 포함",current:"현재 보유 종목 분석",hint:"기존 보유 자산은 동일 종목의 정확한 식별과 필터링에만 사용됩니다. 과거 수익률, 변동성, 집중도와 낙폭은 신규 매수 계산에 영향을 주지 않습니다.",step:"실제 규칙은 신규 매수 예산에만 적용합니다. 데이터 품질, 비용, 현금 보유, 분산, 후보별 한도와 정수 단위를 확인합니다."},
  zh:{label:"已持有投资筛选",exclude:"排除我已经持有的投资",include:"也包含我已经持有的投资",current:"分析我当前的持仓",hint:"现有持仓仅用于精确识别和筛除同一投资。其过去表现、波动率、集中度和回撤不会改变新买入的计算。",step:"实际规则只应用于新的买入预算：数据质量、成本、现金储备、分散、单个候选上限和整数单位。"},
  ar:{label:"مرشح الاستثمارات المملوكة بالفعل",exclude:"استبعاد الاستثمارات التي أملكها بالفعل",include:"إدراج الاستثمارات التي أملكها بالفعل",current:"تحليل مراكزي الحالية",hint:"تُستخدم المراكز الحالية فقط للتعرف الدقيق والتصفية. لا يغيّر أداؤها السابق أو تقلبها أو تركّزها أو تراجعها حساب الشراء الجديد.",step:"يطبّق القواعد العملية على ميزانية الشراء الجديدة فقط: جودة البيانات والتكاليف والاحتياطي النقدي والتنويع وحدود المرشحين والوحدات الكاملة."},
  bg:{label:"Филтър за вече притежавани инвестиции",exclude:"Изключи инвестициите, които вече притежавам",include:"Включи инвестициите, които вече притежавам",current:"Анализирай текущите ми позиции",hint:"Съществуващите позиции се използват само за точно разпознаване и филтриране. Предишната им доходност, волатилност, концентрация и спад не променят изчислението на новата покупка.",step:"Прилага практическите правила само към новия бюджет за покупка: качество на данните, разходи, паричен резерв, диверсификация, лимити за кандидат и цели единици."},
  sk:{label:"Filter už vlastnených investícií",exclude:"Vylúčiť investície, ktoré už vlastním",include:"Zahrnúť investície, ktoré už vlastním",current:"Analyzovať moje súčasné pozície",hint:"Existujúce pozície sa používajú iba na presnú identifikáciu a filtrovanie. Ich minulá výkonnosť, volatilita, koncentrácia a pokles nemenia výpočet nového nákupu.",step:"Použije praktické pravidlá iba na nový rozpočet nákupu: kvalitu údajov, náklady, hotovostnú rezervu, diverzifikáciu, limity kandidátov a celé jednotky."},
  he:{label:"מסנן השקעות שכבר בבעלותי",exclude:"לא לכלול השקעות שכבר בבעלותי",include:"לכלול גם השקעות שכבר בבעלותי",current:"לנתח את ההחזקות הנוכחיות שלי",hint:"החזקות קיימות משמשות רק לזיהוי ולסינון מדויקים. הביצועים, התנודתיות, הריכוז והירידה הקודמים שלהן אינם משנים את חישוב הרכישה החדשה.",step:"מחיל את הכללים המעשיים רק על תקציב הרכישה החדש: איכות נתונים, עלויות, עתודת מזומן, פיזור, מגבלות למועמד ויחידות שלמות."},
  hi:{label:"पहले से रखे निवेशों का फ़िल्टर",exclude:"जो निवेश मेरे पास पहले से हैं उन्हें हटाएँ",include:"जो निवेश मेरे पास पहले से हैं उन्हें भी दिखाएँ",current:"मेरी मौजूदा होल्डिंग का विश्लेषण करें",hint:"मौजूदा होल्डिंग का उपयोग केवल सटीक पहचान और फ़िल्टर के लिए होता है। उनका पिछला प्रदर्शन, उतार-चढ़ाव, एकाग्रता और गिरावट नई खरीद की गणना नहीं बदलते।",step:"व्यावहारिक नियम केवल नई खरीद राशि पर लागू होते हैं: डेटा गुणवत्ता, लागत, नकद रिज़र्व, विविधीकरण, प्रति उम्मीदवार सीमा और पूर्ण इकाइयाँ।"},
  id:{label:"Filter investasi yang sudah dimiliki",exclude:"Kecualikan investasi yang sudah saya miliki",include:"Sertakan investasi yang sudah saya miliki",current:"Analisis kepemilikan saya saat ini",hint:"Kepemilikan yang ada hanya digunakan untuk pencocokan identitas dan penyaringan yang tepat. Kinerja, volatilitas, konsentrasi, dan penurunan sebelumnya tidak mengubah perhitungan pembelian baru.",step:"Menerapkan aturan praktis hanya pada anggaran pembelian baru: kualitas data, biaya, cadangan kas, diversifikasi, batas per kandidat, dan unit utuh."}
};

function languageFor(panel){
  const raw=String(panel?._lang||panel?._haLang||panel?.hass?.language||"en");
  const normalized=raw.toLowerCase();
  if(normalized.startsWith("zh"))return "zh";
  const base=normalized.split("-")[0];
  return COPY[base]?base:"en";
}
function copyFor(panel){return COPY[languageFor(panel)]||COPY.en;}
function escapeHtml(value){return String(value??"").replace(/[&<>"']/g,ch=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[ch]));}

const proto=Panel.prototype;
if(!proto.__investmentContributionOnlyPolicy){
  Object.defineProperty(proto,"__investmentContributionOnlyPolicy",{value:true});

  const fromStored=proto.indicationDraftFromStored;
  proto.indicationDraftFromStored=function(raw){
    const draft=fromStored.call(this,raw);
    const source=raw||{};
    const filter=String(source.portfolio_context||"use")!=="ignore"||String(source.existing_instruments||"allow")==="exclude";
    draft.portfolioContext=filter?"use":"ignore";
    draft.newOnly=filter;
    return draft;
  };

  const capture=proto.captureIndicationDraft;
  proto.captureIndicationDraft=function(form){
    capture.call(this,form);
    if(!this._indicationDraft||!form)return;
    const scope=String(this._indicationDraft.scope||"discover");
    const filter=String(form.querySelector('[name="indication_portfolio_context"]')?.value||"use")!=="ignore";
    this._indicationDraft.portfolioContext=scope==="portfolio"?"use":(filter?"use":"ignore");
    this._indicationDraft.newOnly=scope==="portfolio"?false:filter;
  };

  const preferencesPayload=proto.indicationPreferencesPayload;
  proto.indicationPreferencesPayload=function(draft=this._indicationDraft){
    const payload=preferencesPayload.call(this,draft);
    const source=draft||{};
    const scope=String(source.scope||"discover");
    const filter=scope!=="portfolio"&&String(source.portfolioContext||"use")!=="ignore";
    payload.portfolio_context=scope==="portfolio"?"use":(filter?"use":"ignore");
    payload.existing_instruments=filter?"exclude":"allow";
    payload.overlap_policy="allow";
    payload.overlap_threshold_pct=0;
    payload.existing_cash=0;
    return payload;
  };

  const call=proto.call;
  proto.call=function(message){
    if(message&&["investment/indication","investment/indication_start"].includes(message.type)){
      const draft=this._indicationDraft||{};
      const scope=String(draft.scope||"discover");
      const filter=scope!=="portfolio"&&String(draft.portfolioContext||"use")!=="ignore";
      message={...message,portfolio_context:scope==="portfolio"?"use":(filter?"use":"ignore"),existing_instruments:filter?"exclude":"allow",overlap_policy:"allow",overlap_threshold_pct:0,existing_cash:0};
    }
    return call.call(this,message);
  };

  const modalHtml=proto.indicationModalHtml;
  proto.indicationModalHtml=function(){
    const raw=modalHtml.call(this);
    const template=document.createElement("template");
    template.innerHTML=raw;
    const modal=template.content.querySelector(".indication-modal");
    if(!modal)return raw;
    const c=copyFor(this),scope=String(this._indicationDraft?.scope||"discover");
    modal.insertAdjacentHTML("afterbegin",`<style data-contribution-policy>.owned-filter-hint{grid-column:1/-1;display:block;margin-top:4px;color:var(--secondary-text-color);font-size:10px;line-height:1.42}.contribution-policy-note{margin:8px 0;padding:9px 11px;border:1px solid color-mix(in srgb,var(--primary-color) 24%,var(--divider-color));border-radius:10px;background:color-mix(in srgb,var(--primary-color) 5%,var(--card-background-color));font-size:10px;line-height:1.45;color:var(--secondary-text-color)}</style>`);
    const select=modal.querySelector('[name="indication_portfolio_context"]');
    if(select){
      const field=select.closest("label.setting-field");
      const title=field?.querySelector(":scope > span");
      if(title){title.textContent=c.label;title.classList.remove("help-label","help-open","help-touch");title.removeAttribute("tabindex");title.removeAttribute("role");title.removeAttribute("aria-expanded");}
      const use=select.querySelector('option[value="use"]'),ignore=select.querySelector('option[value="ignore"]');
      if(use)use.textContent=scope==="portfolio"?c.current:c.exclude;
      if(ignore)ignore.textContent=c.include;
      field?.querySelector(".owned-filter-hint")?.remove();
      field?.insertAdjacentHTML("beforeend",`<small class="owned-filter-hint">${escapeHtml(c.hint)}</small>`);
    }
    for(const name of ["indication_new_only","indication_overlap","indication_overlap_threshold","indication_existing_cash"]){
      const control=modal.querySelector(`[name="${name}"]`);
      const field=control?.closest("label.setting-field")||control?.closest(".setting-field");
      field?.remove();
    }
    const explainer=modal.querySelector(".algorithm-explainer");
    const steps=explainer?[...explainer.querySelectorAll("ol>li")]:[];
    if(steps[4])steps[4].textContent=c.step;
    if(explainer&&!modal.querySelector(".contribution-policy-note"))explainer.insertAdjacentHTML("afterend",`<div class="contribution-policy-note">${escapeHtml(c.hint)}</div>`);
    const settingsGrid=modal.querySelector(".indication-glossary .glossary-section .glossary-grid");
    if(settingsGrid){
      const details=[...settingsGrid.querySelectorAll(":scope > details")];
      for(const index of [11,10,8,7])details[index]?.remove();
      const entry=document.createElement("details");
      entry.innerHTML=`<summary>${escapeHtml(c.label)}</summary><p>${escapeHtml(c.hint)}</p>`;
      settingsGrid.insertBefore(entry,settingsGrid.children[7]||null);
    }
    return template.innerHTML;
  };
}
