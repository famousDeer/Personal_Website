/* Statystyki aktywności: cel tygodniowy i seria, kalendarz, rekordy, wykresy.
 *
 * Czyste funkcje bez DOM - działają w przeglądarce (window.ActivityStats)
 * i w Node (testy habits/tests/test_activity_js.py). Liczone w przeglądarce,
 * bo muszą uwzględniać treningi dopisane bez połączenia.
 *
 * Daty to napisy 'RRRR-MM-DD'; w obliczeniach zamieniamy je na numer dnia
 * (dni od 1970-01-01 w UTC), więc zmiana czasu letniego niczego nie przesuwa.
 * Tydzień zaczyna się w poniedziałek.
 */
(function (root, factory) {
    if (typeof module === 'object' && module.exports) {
        module.exports = factory();
    } else {
        root.ActivityStats = factory();
    }
})(typeof self !== 'undefined' ? self : this, function () {
    'use strict';

    var DAY_MS = 86400000;
    var KINDS = ['swim', 'run', 'gym', 'other'];
    var KIND_LABELS = { swim: 'Pływanie', run: 'Bieganie', gym: 'Siłownia', other: 'Inna aktywność' };
    var MONTHS_SHORT = ['sty', 'lut', 'mar', 'kwi', 'maj', 'cze', 'lip', 'sie', 'wrz', 'paź', 'lis', 'gru'];
    var WEEKDAYS_SHORT = ['pn', 'wt', 'śr', 'cz', 'pt', 'sb', 'nd'];
    // Rekord tempa liczymy dopiero od sensownego dystansu - inaczej wygrywa
    // 200-metrowy sprint z rozgrzewki.
    var RUN_PACE_MIN_M = 5000;
    var SWIM_PACE_MIN_M = 400;

    // --- Daty -------------------------------------------------------------
    function dayNumber(iso) {
        var parts = String(iso).split('-');
        return Math.round(Date.UTC(+parts[0], +parts[1] - 1, +parts[2]) / DAY_MS);
    }

    function isoFromDay(day) {
        return new Date(day * DAY_MS).toISOString().slice(0, 10);
    }

    /** Numer dnia poniedziałku tygodnia (1970-01-01 był czwartkiem). */
    function weekStart(day) {
        return day - ((day + 3) % 7);
    }

    function weekdayIndex(day) {
        return (day + 3) % 7;
    }

    function todayIso(now) {
        var d = now || new Date();
        var pad = function (n) { return (n < 10 ? '0' : '') + n; };
        return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
    }

    function dayLabel(iso, withWeekday) {
        var day = dayNumber(iso);
        var date = new Date(day * DAY_MS);
        var text = date.getUTCDate() + ' ' + MONTHS_SHORT[date.getUTCMonth()] + ' ' + date.getUTCFullYear();
        return withWeekday ? WEEKDAYS_SHORT[weekdayIndex(day)] + ', ' + text : text;
    }

    function shortDayLabel(iso) {
        var date = new Date(dayNumber(iso) * DAY_MS);
        return date.getUTCDate() + ' ' + MONTHS_SHORT[date.getUTCMonth()];
    }

    // --- Formatowanie ------------------------------------------------------
    var numberFormat = new Intl.NumberFormat('pl-PL', { maximumFractionDigits: 1 });
    var intFormat = new Intl.NumberFormat('pl-PL', { maximumFractionDigits: 0 });

    function pad2(n) {
        return (n < 10 ? '0' : '') + n;
    }

    /** 1:02:30 albo 28:30. */
    function formatDuration(seconds) {
        if (!seconds) { return ''; }
        seconds = Math.round(seconds);
        var h = Math.floor(seconds / 3600);
        var m = Math.floor((seconds % 3600) / 60);
        var s = seconds % 60;
        return h ? h + ':' + pad2(m) + ':' + pad2(s) : m + ':' + pad2(s);
    }

    /** Suma czasu do kafelków: "5 h 20 min", "45 min". */
    function formatHours(seconds) {
        var minutes = Math.round((seconds || 0) / 60);
        var h = Math.floor(minutes / 60);
        var m = minutes % 60;
        if (!h) { return m + ' min'; }
        return m ? h + ' h ' + m + ' min' : h + ' h';
    }

    function formatDistance(kind, meters) {
        if (!meters) { return ''; }
        if (kind === 'swim') { return intFormat.format(meters) + ' m'; }
        return numberFormat.format(meters / 1000) + ' km';
    }

    /** Tempo: s/km dla biegu, s/100 m dla pływania. */
    function pace(workout) {
        if (!workout.duration || !workout.distance) { return null; }
        if (workout.kind === 'run') { return workout.duration / (workout.distance / 1000); }
        if (workout.kind === 'swim') { return workout.duration / (workout.distance / 100); }
        return null;
    }

    function formatPace(kind, secondsPerUnit) {
        if (!secondsPerUnit) { return ''; }
        var s = Math.round(secondsPerUnit);
        return Math.floor(s / 60) + ':' + pad2(s % 60) + (kind === 'swim' ? '/100 m' : '/km');
    }

    function formatWeight(kg) {
        return numberFormat.format(kg) + ' kg';
    }

    /**
     * Czas wpisany ręcznie -> sekundy. "45" = 45 min, "28:30" = min:s,
     * "1:02:30" = h:min:s, "1h 5min", "1 h", "90 min". Pusty -> null,
     * nieczytelny -> NaN.
     */
    function parseDuration(text) {
        var value = String(text || '').trim().toLowerCase().replace(',', '.');
        if (!value) { return null; }
        var match;
        if ((match = value.match(/^(\d+(?:\.\d+)?)$/))) {
            return Math.round(parseFloat(match[1]) * 60);
        }
        if ((match = value.match(/^(\d+):(\d{1,2})$/))) {
            return +match[1] * 60 + +match[2];
        }
        if ((match = value.match(/^(\d+):(\d{1,2}):(\d{1,2})$/))) {
            return +match[1] * 3600 + +match[2] * 60 + +match[3];
        }
        match = value.match(/^(?:(\d+)\s*h)?\s*(?:(\d+)\s*(?:min|m))?\s*(?:(\d+)\s*s)?$/);
        if (match && (match[1] || match[2] || match[3])) {
            return (+match[1] || 0) * 3600 + (+match[2] || 0) * 60 + (+match[3] || 0);
        }
        return NaN;
    }

    /** Seria: [ćwiczenie, powtórzenia, kg] albo [..., rodzaj ('warmup'...), czas w s]. */
    function isWarmup(row) {
        return row[3] === 'warmup';
    }

    function workingSets(sets) {
        return (sets || []).filter(function (row) { return !isWarmup(row); });
    }

    function setsSummary(sets) {
        // 3 × 8 × 60 kg - kolejne identyczne serie zwinięte w jedną pozycję.
        // Rozgrzewki pomijamy; ćwiczenie na czas (deska) pokazuje sekundy.
        var groups = [];
        workingSets(sets).forEach(function (row) {
            var last = groups[groups.length - 1];
            var seconds = row[4] || null;
            if (last && last.reps === row[1] && last.kg === row[2] && last.seconds === seconds) {
                last.count += 1;
            } else {
                groups.push({ count: 1, reps: row[1], kg: row[2], seconds: seconds });
            }
        });
        return groups.map(function (g) {
            var prefix = g.count > 1 ? g.count + ' × ' : '';
            if (!g.reps && g.seconds) {
                return prefix + formatDuration(g.seconds) + (g.kg ? ' × ' + formatWeight(g.kg) : '');
            }
            var text = prefix + (g.reps || '–');
            return g.kg ? text + ' × ' + formatWeight(g.kg) : text + ' powt.';
        }).join(', ');
    }

    function exercisesOf(workout) {
        var names = [];
        (workout.sets || []).forEach(function (row) {
            if (names.indexOf(row[0]) === -1) { names.push(row[0]); }
        });
        return names;
    }

    /** Jedna linia opisu treningu (podpowiedź w kalendarzu, lista). */
    function summarize(workout) {
        var parts = [workout.title || KIND_LABELS[workout.kind] || 'Trening'];
        var distance = formatDistance(workout.kind, workout.distance);
        if (distance) { parts.push(distance); }
        if (workout.duration) { parts.push(formatDuration(workout.duration)); }
        var p = pace(workout);
        if (p) { parts.push(formatPace(workout.kind, p)); }
        var working = workingSets(workout.sets);
        if (workout.kind === 'gym' && working.length) {
            var names = exercisesOf(workout);
            parts.push(names.length + ' ' + plural(names.length, 'ćwiczenie', 'ćwiczenia', 'ćwiczeń')
                + ', ' + working.length + ' ' + plural(working.length, 'seria', 'serie', 'serii'));
        }
        return parts.join(' · ');
    }

    function plural(n, one, few, many) {
        if (n === 1) { return one; }
        var lastDigit = n % 10;
        var lastTwo = n % 100;
        if (lastDigit >= 2 && lastDigit <= 4 && (lastTwo < 12 || lastTwo > 14)) { return few; }
        return many;
    }

    // --- Cel tygodniowy i seria -------------------------------------------
    function activeDays(workouts) {
        var days = {};
        workouts.forEach(function (w) { days[dayNumber(w.date)] = true; });
        return days;
    }

    function weekCounts(days) {
        var counts = {};
        Object.keys(days).forEach(function (day) {
            var start = weekStart(+day);
            counts[start] = (counts[start] || 0) + 1;
        });
        return counts;
    }

    /**
     * Cel liczy DNI z treningiem (bieg i siłownia jednego dnia to jeden dzień),
     * a seria to tygodnie z rzędu z osiągniętym celem. Bieżący tydzień nie
     * przerywa serii, dopóki się nie skończył.
     */
    function weeklyGoal(workouts, goal, today) {
        goal = Math.max(1, goal || 3);
        var todayDay = dayNumber(today);
        var current = weekStart(todayDay);
        var past = workouts.filter(function (w) { return dayNumber(w.date) <= todayDay; });
        var days = activeDays(past);
        var counts = weekCounts(days);
        var thisWeek = counts[current] || 0;

        var streak = 0;
        var week = thisWeek >= goal ? current : current - 7;
        while ((counts[week] || 0) >= goal) {
            streak += 1;
            week -= 7;
        }

        var weeks = Object.keys(counts).map(Number).sort(function (a, b) { return a - b; });
        var best = 0;
        var run = 0;
        if (weeks.length) {
            for (week = weeks[0]; week <= current; week += 7) {
                run = (counts[week] || 0) >= goal ? run + 1 : 0;
                best = Math.max(best, run);
            }
        }

        var byDay = {};
        past.forEach(function (w) {
            var day = dayNumber(w.date);
            (byDay[day] = byDay[day] || []).push(w.kind);
        });
        var weekDays = [];
        for (var i = 0; i < 7; i++) {
            weekDays.push({
                iso: isoFromDay(current + i),
                kinds: byDay[current + i] || [],
                isToday: current + i === todayDay,
                isFuture: current + i > todayDay
            });
        }

        var lastDay = Object.keys(days).map(Number).sort(function (a, b) { return b - a; })[0];
        var previousWeek = counts[current - 7] || 0;
        return {
            goal: goal,
            done: thisWeek,
            missing: Math.max(0, goal - thisWeek),
            met: thisWeek >= goal,
            // Dni do końca tygodnia łącznie z dzisiejszym; dzień z treningiem
            // już się nie liczy jako szansa.
            daysLeft: 7 - weekdayIndex(todayDay) - (byDay[todayDay] ? 1 : 0),
            streak: streak,
            bestStreak: best,
            // Seria sprzed tego tygodnia, którą można jeszcze uratować.
            streakAtRisk: thisWeek < goal && streak > 0,
            previousWeek: previousWeek,
            weekDays: weekDays,
            lastIso: lastDay === undefined ? null : isoFromDay(lastDay),
            daysSinceLast: lastDay === undefined ? null : todayDay - lastDay,
            total: workouts.length
        };
    }

    /** Jedno zdanie zachęty dopasowane do sytuacji w tygodniu. */
    function motivation(info) {
        if (!info.total) {
            return 'Zacznij od jednego treningu – nawet 20 minut się liczy.';
        }
        if (info.met) {
            var weeks = info.streak + ' ' + plural(info.streak, 'tydzień', 'tygodnie', 'tygodni');
            return info.done > info.goal
                ? 'Cel tygodnia przekroczony. Seria: ' + weeks + ' z rzędu.'
                : 'Cel tygodnia osiągnięty! Seria: ' + weeks + ' z rzędu.';
        }
        var missing = info.missing + ' ' + plural(info.missing, 'dnia', 'dni', 'dni');
        if (info.missing > info.daysLeft) {
            return 'Tego tygodnia już nie domkniesz, ale każdy trening buduje formę. Nowa seria zaczyna się w poniedziałek.';
        }
        var risk = info.streakAtRisk
            ? ' Na szali seria ' + info.streak + ' ' + plural(info.streak, 'tygodnia', 'tygodni', 'tygodni') + '.'
            : '';
        if (info.missing === info.daysLeft) {
            return 'Brakuje ' + missing + ' z treningiem – trening każdego dnia do niedzieli domyka cel.' + risk;
        }
        if (info.daysSinceLast !== null && info.daysSinceLast >= 3) {
            return 'Od ' + info.daysSinceLast + ' dni bez treningu. Krótki trening dziś przerwie przestój.' + risk;
        }
        return 'Brakuje ' + missing + ' z treningiem do celu, do końca tygodnia ' +
            info.daysLeft + ' ' + plural(info.daysLeft, 'dzień', 'dni', 'dni') + '.' + risk;
    }

    // --- Kalendarz aktywności ---------------------------------------------
    function mainKind(dayWorkouts) {
        var best = dayWorkouts[0];
        dayWorkouts.forEach(function (w) {
            if ((w.duration || 0) > (best.duration || 0)) { best = w; }
        });
        return best.kind;
    }

    /**
     * Siatka tygodni (kolumny) × dni (wiersze), kończy się na bieżącym tygodniu.
     * Kolor dnia = dyscyplina najdłuższego treningu tego dnia.
     */
    function heatmap(workouts, today, weeksCount) {
        weeksCount = weeksCount || 53;
        var todayDay = dayNumber(today);
        var last = weekStart(todayDay);
        var first = last - (weeksCount - 1) * 7;
        var byDay = {};
        workouts.forEach(function (w) {
            var day = dayNumber(w.date);
            if (day >= first && day <= todayDay) { (byDay[day] = byDay[day] || []).push(w); }
        });
        var weeks = [];
        var previousMonth = null;
        for (var start = first; start <= last; start += 7) {
            var month = new Date(start * DAY_MS).getUTCMonth();
            var days = [];
            for (var i = 0; i < 7; i++) {
                var day = start + i;
                var list = byDay[day] || [];
                days.push({
                    iso: isoFromDay(day),
                    workouts: list,
                    kind: list.length ? mainKind(list) : null,
                    future: day > todayDay
                });
            }
            weeks.push({
                start: isoFromDay(start),
                monthLabel: month !== previousMonth ? MONTHS_SHORT[month] : '',
                days: days
            });
            previousMonth = month;
        }
        return { weeks: weeks, activeDays: Object.keys(byDay).length };
    }

    // --- Porównanie miesięcy ----------------------------------------------
    function totals(workouts) {
        var result = { count: 0, days: 0, duration: 0, runDistance: 0, swimDistance: 0, gymSessions: 0 };
        var days = {};
        workouts.forEach(function (w) {
            result.count += 1;
            days[w.date] = true;
            result.duration += w.duration || 0;
            if (w.kind === 'run') { result.runDistance += w.distance || 0; }
            if (w.kind === 'swim') { result.swimDistance += w.distance || 0; }
            if (w.kind === 'gym') { result.gymSessions += 1; }
        });
        result.days = Object.keys(days).length;
        return result;
    }

    /** Ten miesiąc do dziś kontra ten sam fragment poprzedniego miesiąca. */
    function monthComparison(workouts, today) {
        var parts = today.split('-').map(Number);
        var year = parts[0], month = parts[1], dayOfMonth = parts[2];
        var prevYear = month === 1 ? year - 1 : year;
        var prevMonth = month === 1 ? 12 : month - 1;
        var prevLength = new Date(Date.UTC(prevYear, prevMonth, 0)).getUTCDate();
        var pad = function (n) { return (n < 10 ? '0' : '') + n; };
        var curStart = year + '-' + pad(month) + '-01';
        var prevStart = prevYear + '-' + pad(prevMonth) + '-01';
        var prevEnd = prevYear + '-' + pad(prevMonth) + '-' + pad(Math.min(dayOfMonth, prevLength));
        return {
            current: totals(workouts.filter(function (w) { return w.date >= curStart && w.date <= today; })),
            previous: totals(workouts.filter(function (w) { return w.date >= prevStart && w.date <= prevEnd; })),
            monthName: MONTHS_SHORT[month - 1],
            previousName: MONTHS_SHORT[prevMonth - 1]
        };
    }

    // --- Serie do wykresów -------------------------------------------------
    /** Tygodniowa objętość: dystans (bieg, pływanie) albo liczba treningów. */
    function weeklyVolume(workouts, kind, today, weeksCount) {
        weeksCount = weeksCount || 16;
        var last = weekStart(dayNumber(today));
        var first = last - (weeksCount - 1) * 7;
        var values = {};
        workouts.forEach(function (w) {
            if (w.kind !== kind) { return; }
            var start = weekStart(dayNumber(w.date));
            if (start < first || start > last) { return; }
            values[start] = (values[start] || 0) + (kind === 'gym' ? 1 : (w.distance || 0));
        });
        var result = [];
        for (var start = first; start <= last; start += 7) {
            result.push({ week: isoFromDay(start), value: values[start] || 0 });
        }
        return result;
    }

    function byDate(a, b) {
        return a.date < b.date ? -1 : a.date > b.date ? 1 : ((a.time || '') < (b.time || '') ? -1 : 1);
    }

    /** Tempo kolejnych treningów (ostatnie `limit`). */
    function paceSeries(workouts, kind, limit) {
        return workouts
            .filter(function (w) { return w.kind === kind && pace(w); })
            .sort(byDate)
            .slice(-(limit || 30))
            .map(function (w) { return { date: w.date, pace: pace(w), distance: w.distance, workout: w }; });
    }

    /** Ćwiczenia siłowe od najczęściej wykonywanych. */
    function exerciseNames(workouts) {
        var counts = {};
        workouts.forEach(function (w) {
            if (w.kind !== 'gym') { return; }
            exercisesOf(w).forEach(function (name) { counts[name] = (counts[name] || 0) + 1; });
        });
        return Object.keys(counts).sort(function (a, b) {
            return counts[b] - counts[a] || a.localeCompare(b, 'pl');
        });
    }

    /** Najcięższa seria ćwiczenia na każdym treningu + objętość (powt. × kg). */
    function exerciseProgress(workouts, name) {
        return workouts
            .filter(function (w) { return w.kind === 'gym'; })
            .sort(byDate)
            .map(function (w) {
                var sets = workingSets(w.sets).filter(function (row) { return row[0] === name; });
                if (!sets.length) { return null; }
                var maxWeight = 0;
                var volume = 0;
                var maxReps = 0;
                sets.forEach(function (row) {
                    maxWeight = Math.max(maxWeight, row[2] || 0);
                    maxReps = Math.max(maxReps, row[1] || 0);
                    volume += (row[1] || 0) * (row[2] || 0);
                });
                return {
                    date: w.date,
                    maxWeight: maxWeight,
                    maxReps: maxReps,
                    volume: volume,
                    summary: setsSummary(sets)
                };
            })
            .filter(Boolean);
    }

    // --- Rekordy -----------------------------------------------------------
    function maxBy(list, score) {
        var best = null;
        var bestScore = -Infinity;
        list.forEach(function (item) {
            var value = score(item);
            if (value !== null && value !== undefined && value > bestScore) {
                best = item;
                bestScore = value;
            }
        });
        return best;
    }

    function records(workouts) {
        var runs = workouts.filter(function (w) { return w.kind === 'run'; });
        var swims = workouts.filter(function (w) { return w.kind === 'swim'; });
        var fastest = function (list, minimum) {
            return maxBy(list.filter(function (w) { return (w.distance || 0) >= minimum && pace(w); }),
                function (w) { return -pace(w); });
        };
        var lifts = {};
        workouts.forEach(function (w) {
            if (w.kind !== 'gym') { return; }
            workingSets(w.sets).forEach(function (row) {
                var kg = row[2] || 0;
                var current = lifts[row[0]];
                if (kg > 0 && (!current || kg > current.weight || (kg === current.weight && (row[1] || 0) > current.reps))) {
                    lifts[row[0]] = { exercise: row[0], weight: kg, reps: row[1] || 0, date: w.date };
                }
            });
        });
        return {
            run: {
                longest: maxBy(runs, function (w) { return w.distance || null; }),
                fastest: fastest(runs, RUN_PACE_MIN_M),
                fastestMinimum: RUN_PACE_MIN_M
            },
            swim: {
                longest: maxBy(swims, function (w) { return w.distance || null; }),
                fastest: fastest(swims, SWIM_PACE_MIN_M),
                fastestMinimum: SWIM_PACE_MIN_M
            },
            gym: Object.keys(lifts).map(function (key) { return lifts[key]; })
                .sort(function (a, b) { return b.weight - a.weight; })
        };
    }

    // --- Zdrowie i forma (dane dzienne z zegarka) ---------------------------
    var TRAINING_STATUS = {
        PRODUCTIVE: 'Produktywny', MAINTAINING: 'Utrzymanie formy', RECOVERY: 'Regeneracja',
        UNPRODUCTIVE: 'Nieproduktywny', DETRAINING: 'Spadek formy', PEAKING: 'Szczyt formy',
        OVERREACHING: 'Przeciążenie', STRAINED: 'Przemęczenie', NO_STATUS: 'Brak statusu'
    };
    var READINESS_LEVELS = { PRIME: 'Świetna', HIGH: 'Wysoka', MODERATE: 'Umiarkowana', LOW: 'Niska', POOR: 'Bardzo niska' };
    var HRV_STATUS = { BALANCED: 'w normie', UNBALANCED: 'poza normą', LOW: 'niskie', POOR: 'bardzo niskie' };
    var TE_LABELS = {
        RECOVERY: 'Regeneracja', AEROBIC_BASE: 'Baza tlenowa', TEMPO: 'Tempo',
        LACTATE_THRESHOLD: 'Próg mleczanowy', VO2MAX: 'VO2max', ANAEROBIC_CAPACITY: 'Pojemność beztlenowa',
        SPRINT: 'Sprint', NO_BENEFIT: 'Bez efektu'
    };

    function label(map, key) {
        if (!key) { return ''; }
        return map[key] || map[String(key).replace(/_\d+$/, '')] ||
            String(key).toLowerCase().replace(/_/g, ' ');
    }

    /** Najnowszy dzień z wartością `key`: {date, value} albo null. */
    function latestHealth(health, key) {
        for (var i = (health || []).length - 1; i >= 0; i--) {
            var value = health[i][key];
            if (value !== undefined && value !== null && value !== '') {
                return { date: health[i].date, value: value };
            }
        }
        return null;
    }

    /** Dzień po dniu z ostatnich `days` dni (brak pomiaru = null). */
    function healthSeries(health, key, today, days) {
        var byDate = {};
        (health || []).forEach(function (row) { byDate[row.date] = row[key]; });
        var end = dayNumber(today);
        var result = [];
        for (var day = end - (days || 30) + 1; day <= end; day++) {
            var iso = isoFromDay(day);
            var value = byDate[iso];
            result.push({ date: iso, value: value === undefined ? null : value });
        }
        return result;
    }

    function average(values) {
        var list = values.filter(function (v) { return v !== null && v !== undefined; });
        if (!list.length) { return null; }
        return list.reduce(function (a, b) { return a + b; }, 0) / list.length;
    }

    /** VO2max w czasie: z danych dziennych i z biegów (zegarek liczy go po biegu). */
    function vo2Series(workouts, health) {
        var byDate = {};
        (workouts || []).forEach(function (w) {
            if (w.details && w.details.vo2max) { byDate[w.date] = w.details.vo2max; }
        });
        (health || []).forEach(function (row) {
            if (row.vo2max) { byDate[row.date] = row.vo2max; }
        });
        return Object.keys(byDate).sort().map(function (date) { return { date: date, value: byDate[date] }; });
    }

    /** Wartość sprzed ok. `days` dni (do porównania „↑ 1,2 w 90 dni”). */
    function valueBefore(series, today, days) {
        var limit = isoFromDay(dayNumber(today) - days);
        var found = null;
        series.forEach(function (point) { if (point.date <= limit) { found = point; } });
        return found;
    }

    function formatRaceTime(seconds) {
        return seconds ? formatDuration(seconds) : '';
    }

    function formatSleep(seconds) {
        if (!seconds) { return ''; }
        var minutes = Math.round(seconds / 60);
        return Math.floor(minutes / 60) + ' h ' + pad2(minutes % 60) + ' min';
    }

    /** Udział stref tętna w procentach (zaokrąglone, suma ~100). */
    function zoneShares(zones) {
        var total = (zones || []).reduce(function (a, b) { return a + (b || 0); }, 0);
        if (!total) { return []; }
        return zones.map(function (value) { return Math.round((value || 0) / total * 100); });
    }

    function lapPace(kind, lap) {
        if (!lap.d || !lap.t) { return ''; }
        return formatPace(kind, kind === 'swim' ? lap.t / (lap.d / 100) : lap.t / (lap.d / 1000));
    }

    return {
        TRAINING_STATUS: TRAINING_STATUS,
        READINESS_LEVELS: READINESS_LEVELS,
        HRV_STATUS: HRV_STATUS,
        TE_LABELS: TE_LABELS,
        label: label,
        latestHealth: latestHealth,
        healthSeries: healthSeries,
        average: average,
        vo2Series: vo2Series,
        valueBefore: valueBefore,
        formatRaceTime: formatRaceTime,
        formatSleep: formatSleep,
        zoneShares: zoneShares,
        lapPace: lapPace,
        isWarmup: isWarmup,
        workingSets: workingSets,
        KINDS: KINDS,
        KIND_LABELS: KIND_LABELS,
        WEEKDAYS_SHORT: WEEKDAYS_SHORT,
        dayNumber: dayNumber,
        isoFromDay: isoFromDay,
        weekStart: weekStart,
        todayIso: todayIso,
        dayLabel: dayLabel,
        shortDayLabel: shortDayLabel,
        plural: plural,
        formatDuration: formatDuration,
        formatHours: formatHours,
        formatDistance: formatDistance,
        formatPace: formatPace,
        formatWeight: formatWeight,
        pace: pace,
        parseDuration: parseDuration,
        setsSummary: setsSummary,
        exercisesOf: exercisesOf,
        summarize: summarize,
        weeklyGoal: weeklyGoal,
        motivation: motivation,
        heatmap: heatmap,
        totals: totals,
        monthComparison: monthComparison,
        weeklyVolume: weeklyVolume,
        paceSeries: paceSeries,
        exerciseNames: exerciseNames,
        exerciseProgress: exerciseProgress,
        records: records
    };
});
