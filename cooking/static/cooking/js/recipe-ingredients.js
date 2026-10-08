/* =========================================================================
   Składniki przepisu: wyszukiwanie w spiżarni i rozbiór wklejonej listy
   =========================================================================
   Czysta logika bez DOM - używa jej recipe-form.js, a testy uruchamiają ją
   w Node (cooking/tests_recipe_js.py). Udostępnia window.RecipeIngredients.

   - search(query, index)      podpowiedzi nazw ze spiżarni (produkty i grupy)
   - exact(name, index)        produkt/grupa o tej samej nazwie - tak dopasowuje
                               „Gotuj” (bez rozróżniania wielkości liter)
   - closest(name, index)      najbliższa nazwa mimo odmiany i literówek
                               („mięsa mielonego” -> „Mięso mielone”)
   - parseLine / parseList     „500 g mięsa mielonego”, „Cebula - 1 szt.”,
                               „2 łyżki oliwy”, „szczypta soli”, „sól do smaku”
   - amountText(q, unit)       „500 g”, „2 łyżki”, „5 łyżek”, „do smaku”
   - categoryFor(name, rules)  kategoria z reguł słów kluczowych (jak serwer)
   - sameFamily(a, b)          czy jednostki dają się przeliczyć (g↔kg, ml↔l)
   ========================================================================= */
(function (root) {
    'use strict';

    // --- tekst ------------------------------------------------------------
    function fold(text) {
        return String(text || '')
            .toLowerCase()
            .replace(/ł/g, 'l')
            .normalize('NFD')
            .replace(/[̀-ͯ]/g, '')
            .replace(/[^a-z0-9]+/g, ' ')
            .trim();
    }

    function sameName(text) {
        return String(text || '').toLocaleLowerCase('pl').replace(/\s+/g, ' ').trim();
    }

    function tokens(text) {
        return fold(text).split(' ').filter((token) => token && !/^\d+$/.test(token));
    }

    // Ten sam wyraz w innej formie: wspólny początek bez 1-2 ostatnich liter
    // (cebula/cebuli/cebule, mięso/mięsa, mielone/mielonego, jajka/jaj).
    function stemEqual(a, b) {
        if (a === b) {
            return true;
        }
        const shorter = Math.min(a.length, b.length);
        if (shorter < 3) {
            return false;
        }
        const need = Math.max(3, shorter - 2);
        return a.slice(0, need) === b.slice(0, need);
    }

    // 0..2: 2 = te same wyrazy, >0 = jedna nazwa zawiera drugą (w odmianie).
    function nameScore(candidate, text) {
        const a = tokens(candidate);
        const b = tokens(text);
        if (!a.length || !b.length) {
            return 0;
        }
        const used = new Set();
        let matched = 0;
        a.forEach((word) => {
            const index = b.findIndex((other, position) => !used.has(position) && stemEqual(word, other));
            if (index >= 0) {
                used.add(index);
                matched += 1;
            }
        });
        const shorter = Math.min(a.length, b.length);
        if (matched < shorter) {
            return 0;
        }
        const longer = Math.max(a.length, b.length);
        return matched === longer ? 2 : matched / longer;
    }

    function exact(name, index) {
        const wanted = sameName(name);
        return wanted ? (index || []).find((entry) => sameName(entry.name) === wanted) || null : null;
    }

    function closest(name, index) {
        if (!fold(name)) {
            return null;
        }
        let best = null;
        let bestScore = 0;
        (index || []).forEach((entry) => {
            let score = nameScore(entry.name, name);
            if (score && entry.kind === 'group') {
                score += 0.05;  // ogólna nazwa przed konkretną marką
            }
            if (score > bestScore) {
                best = entry;
                bestScore = score;
            }
        });
        return best && bestScore >= 0.5 ? { entry: best, score: bestScore, sure: bestScore >= 2 } : null;
    }

    function search(query, index, limit) {
        const folded = fold(query);
        if (!folded) {
            return [];
        }
        const queryWords = folded.split(' ');
        const ranked = [];
        (index || []).forEach((entry) => {
            const name = fold(entry.name);
            const words = name.split(' ');
            let rank = -1;
            if (name.startsWith(folded)) {
                rank = 0;
            } else if (queryWords.every((word) => words.some((other) => other.startsWith(word)))) {
                rank = 1;
            } else if (nameScore(entry.name, query) > 0) {
                rank = 2;
            } else if (name.includes(folded)) {
                rank = 3;
            }
            if (rank >= 0) {
                ranked.push({ entry, rank, group: entry.kind === 'group' ? 0 : 1 });
            }
        });
        ranked.sort((a, b) => a.rank - b.rank || a.group - b.group
            || a.entry.name.localeCompare(b.entry.name, 'pl'));
        return ranked.slice(0, limit || 6).map((item) => item.entry);
    }

    // --- jednostki ----------------------------------------------------------
    const FAMILY = { g: 'mass', kg: 'mass', ml: 'volume', l: 'volume', szt: 'piece', opak: 'pack' };

    function sameFamily(a, b) {
        return Boolean(FAMILY[a]) && FAMILY[a] === FAMILY[b];
    }

    // [wzorzec (po fold), jednostka, mnożnik, nazwa kuchenna do notatki]
    const UNIT_WORDS = [
        [/^(g|gr|gram|grama|gramy|gramow)$/, 'g', 1],
        [/^(dag|dkg|deko|dekagram\w*)$/, 'g', 10],
        [/^(kg|kilo|kilogram\w*)$/, 'kg', 1],
        [/^(ml|mililitr\w*)$/, 'ml', 1],
        [/^(l|litr|litra|litry|litrow)$/, 'l', 1],
        [/^(szt|sztuka|sztuki|sztuk|sztuke)$/, 'szt', 1],
        [/^(opak|opakowanie|opakowania|opakowan|paczka|paczki|paczek|paczke|puszka|puszki|puszek|puszke|sloik|sloika|sloiki|sloikow|kostka|kostki|kostek|kostke)$/, 'opak', 1],
        [/^(lyzka|lyzki|lyzek|lyzke|lyzk)$/, 'lyzka', 1],
        [/^(lyzeczka|lyzeczki|lyzeczek|lyzeczke)$/, 'lyzeczka', 1],
        [/^(szklanka|szklanki|szklanek|szklanke)$/, 'szklanka', 1],
        [/^(szczypta|szczypty|szczypt|szczypte)$/, 'szczypta', 1],
        [/^(zabek|zabki|zabkow|zabka)$/, 'szt', 1, 'ząbek'],
    ];
    const PINCH = /^\s*(szczypt\w*)\s+/i;
    const NO_AMOUNT = /\b(do smaku|odrobin\w*|troch\w*|garsc|garsci|opcjonalnie|wedlug uznania)\b/;

    // Odmiana jednostek kuchennych: [1, 2-4, 5+, ułamek]
    const FORMS = {
        lyzka: ['łyżka', 'łyżki', 'łyżek', 'łyżki'],
        lyzeczka: ['łyżeczka', 'łyżeczki', 'łyżeczek', 'łyżeczki'],
        szklanka: ['szklanka', 'szklanki', 'szklanek', 'szklanki'],
        szczypta: ['szczypta', 'szczypty', 'szczypt', 'szczypty'],
    };
    const LABELS = { szt: 'szt.', opak: 'opak.', do_smaku: 'do smaku' };
    const FRACTIONS = { '½': 0.5, '¼': 0.25, '¾': 0.75, '⅓': 1 / 3, '⅔': 2 / 3, '⅛': 0.125 };
    const NUMBER = String.raw`\d+\s+\d+\/\d+|\d+\/\d+|\d+(?:[.,]\d+)?`;

    function toNumber(raw) {
        let text = String(raw).trim();
        let value = 0;
        const mixed = text.match(/^(\d+)\s+(\d+)\/(\d+)$/);
        if (mixed) {
            return Number(mixed[1]) + Number(mixed[2]) / Number(mixed[3]);
        }
        const fraction = text.match(/^(\d+)\/(\d+)$/);
        if (fraction) {
            return Number(fraction[1]) / Number(fraction[2]);
        }
        text = text.replace(',', '.');
        value = Number(text);
        return Number.isFinite(value) ? value : NaN;
    }

    function formatQuantity(value) {
        const rounded = Math.round(value * 100) / 100;
        return String(rounded).replace('.', ',');
    }

    function unitFor(word) {
        const folded = fold(word);
        for (const [pattern, unit, factor, kitchen] of UNIT_WORDS) {
            if (pattern.test(folded)) {
                return { unit, factor, kitchen: kitchen || '' };
            }
        }
        return null;
    }

    function cleanName(text) {
        let name = String(text || '')
            .replace(/\([^)]*\)/g, ' ')
            .replace(/\b(do smaku|opcjonalnie|według uznania)\b/gi, ' ')
            .replace(/^[\s:–—-]+|[\s:,;.–—-]+$/g, '')
            .replace(/\s+/g, ' ')
            .trim();
        name = name.replace(/^(odrobin\w*|troch\w*|garś[cć]\w*)\s+/i, '');
        return name ? name.charAt(0).toLocaleUpperCase('pl') + name.slice(1) : '';
    }

    function parseLine(line) {
        let text = String(line || '').trim();
        Object.keys(FRACTIONS).forEach((symbol) => {
            text = text.replace(new RegExp(`(\\d)\\s*${symbol}`, 'g'), (_, digit) => `${digit}.${String(FRACTIONS[symbol]).split('.')[1]}`)
                .replace(new RegExp(symbol, 'g'), String(FRACTIONS[symbol]));
        });
        // Wypunktowanie: „- ”, „• ”, „1. 500 g …”
        text = text.replace(/^\s*(?:[-–—•*·▪●○]+|\d+[.)](?=\s+\d))\s*/, '').trim();
        if (!text) {
            return null;
        }
        const original = text;
        if (PINCH.test(text) && !/^\d/.test(text)) {
            text = `1 ${text}`;  // „szczypta soli” = 1 szczypta
        }
        let quantity = null;
        let unitWord = '';
        let name = text;

        const first = text.match(new RegExp(`^(${NUMBER})(?:\\s*[-–]\\s*(?:${NUMBER}))?\\s*([^\\s\\d.,]+\\.?)?\\s*(.*)$`, 'u'));
        const last = text.match(new RegExp(`^(.*?)[\\s:–—-]+(${NUMBER})(?:\\s*[-–]\\s*(?:${NUMBER}))?\\s*([^\\s\\d.,]+)?\\.?$`, 'u'));
        if (first) {
            quantity = toNumber(first[1]);
            unitWord = (first[2] || '').replace(/\.$/, '');
            name = first[3];
            if (unitWord && !unitFor(unitWord)) {
                // „2 cebule” - drugie słowo to już nazwa
                name = `${unitWord} ${name}`.trim();
                unitWord = '';
            }
        } else if (last && cleanName(last[1])) {
            quantity = toNumber(last[2]);
            unitWord = last[3] || '';
            name = last[1];
            if (unitWord && !unitFor(unitWord)) {
                name = `${name} ${unitWord}`;
                unitWord = '';
            }
        }

        const unit = unitWord ? unitFor(unitWord) : null;
        const result = { original, name: cleanName(name), quantity: '', unit: 'szt', note: '', unitGiven: Boolean(unit) };
        if (!result.name) {
            return null;
        }
        if (quantity !== null && Number.isFinite(quantity) && quantity > 0) {
            if (unit) {
                result.unit = unit.unit;
                result.quantity = formatQuantity(quantity * unit.factor);
                if (unit.kitchen) {
                    result.note = `${unit.kitchen} → szt.`;
                } else if (unit.factor !== 1) {
                    result.note = `${formatQuantity(quantity)} dag = ${result.quantity} g`;
                }
            } else {
                result.quantity = formatQuantity(quantity);
            }
        } else {
            // Bez ilości: „sól do smaku”, „pieprz”, „garść rukoli”.
            result.unit = 'do_smaku';
            if (!NO_AMOUNT.test(fold(original))) {
                result.note = 'bez ilości - ustawiono „do smaku”';
            }
        }
        return result;
    }

    function parseList(text, index) {
        return String(text || '')
            .split(/\r?\n|;/)
            .map(parseLine)
            .filter(Boolean)
            .map((item) => {
                const known = exact(item.name, index);
                const near = known ? null : closest(item.name, index);
                if (known) {
                    item.match = known;
                    item.name = known.name;
                } else if (near && near.sure) {
                    item.match = near.entry;
                    item.renamed = item.name;
                    item.name = near.entry.name;
                }
                return item;
            });
    }

    // --- kategorie (jak cooking.services.categories.match_text) -------------
    function searchable(text) {
        return String(text || '').toLowerCase().replace(/[^\p{L}\p{N}_]+/gu, ' ').trim().replace(/\s+/g, ' ');
    }

    function hasKeyword(text, keyword) {
        const prefix = keyword.endsWith('*');
        const body = searchable(keyword.replace(/\*$/, ''));
        if (!body) {
            return false;
        }
        const padded = ` ${text} `;
        return prefix ? padded.includes(` ${body}`) : padded.includes(` ${body} `);
    }

    function categoryFor(name, rules) {
        const text = searchable(name);
        if (!text) {
            return '';
        }
        for (const [category, keywords] of rules || []) {
            if (keywords.some((keyword) => hasKeyword(text, keyword))) {
                return category;
            }
        }
        return '';
    }

    function unitLabel(unit, quantity) {
        const forms = FORMS[unit];
        const value = Number(String(quantity || '').replace(',', '.'));
        if (!forms) {
            return LABELS[unit] || unit;
        }
        if (!Number.isFinite(value) || !value) {
            return forms[0];
        }
        if (!Number.isInteger(value)) {
            return forms[3];
        }
        if (value === 1) {
            return forms[0];
        }
        const tens = value % 100;
        return value % 10 >= 2 && value % 10 <= 4 && (tens < 12 || tens > 14) ? forms[1] : forms[2];
    }

    function amountText(quantity, unit) {
        if (unit === 'do_smaku') {
            return 'do smaku';
        }
        const amount = String(quantity || '').trim();
        return amount ? `${amount} ${unitLabel(unit, amount)}` : `? ${unitLabel(unit, '')}`;
    }

    const api = {
        fold, sameName, search, exact, closest, nameScore, parseLine, parseList, categoryFor, sameFamily,
        unitLabel, amountText,
    };
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = api;
    }
    root.RecipeIngredients = api;
}(typeof window !== 'undefined' ? window : globalThis));
