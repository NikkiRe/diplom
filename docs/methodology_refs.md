# Методические источники протокола экспериментов

Список источников, на которые опирается `experiments/PROTOCOL.md`. Записи оформлены по
ГОСТ Р 7.0.100-2018. После каждой записи указано, что из источника взято в протокол, и как выходные
данные сверены с первоисточником (дата сверки — 06.10.2026). Короткие метки в квадратных скобках
совпадают с метками в тексте протокола.

Сверка: метаданные (авторы, название, издание, страницы, DOI) сравнивались с Crossref или OpenAlex,
а место, даты конференции и ISBN — с первой страницей самой статьи (авторская или репозиторная копия
PDF). Записи, которые удалось сверить только частично, отмечены.

## А. Методика измерения производительности

1. **[Georges 2007]** Georges, A. Statistically Rigorous Java Performance Evaluation / A. Georges,
   D. Buytaert, L. Eeckhout. – Текст : электронный // OOPSLA '07 : Proceedings of the 22nd Annual ACM
   SIGPLAN Conference on Object-Oriented Programming Systems, Languages and Applications (Montréal,
   Québec, Canada, October 21–25, 2007). – New York : ACM, 2007. – P. 57–76. – DOI 10.1145/1297027.1297033.

   *Что взято.* Распространённые способы сводки («лучший из N», среднее без ДИ, одна инвокация ВМ)
   ведут к ошибочным выводам. Отсюда запрет «лучшего из N» и учёт каждого замера (PROTOCOL §0.3).
   Разделение режимов «старт» и «установившийся режим»: многократные независимые инвокации и ДИ по
   инвокациям. В протоколе это свежий процесс на блок и холодный первый шаг как отдельная метрика (§2.6, E2).
   Наблюдение, что первая инвокация меняет состояние системы (например, дисковый кэш), которое переживает
   её, а потому её исключают или контролируют. Отсюда прогрев и явное управление страничным кэшем
   (§2.5, приложение C). Окно из k итераций с порогом коэффициента вариации как признак установившегося режима.

   *Проверено:* Crossref (DOI, страницы); первая страница авторской копии PDF (место, даты, ISBN
   978-1-59593-786-5); разделы 4.1–4.2 о методиках старта и установившегося режима.

2. **[Hoefler 2015]** Hoefler, T. Scientific Benchmarking of Parallel Computing Systems : Twelve Ways to
   Tell the Masses When Reporting Performance Results / T. Hoefler, R. Belli. – Текст : электронный //
   SC '15 : Proceedings of the International Conference for High Performance Computing, Networking,
   Storage and Analysis (Austin, TX, USA, November 15–20, 2015). – New York : ACM, 2015. – Art. 73. –
   P. 1–12. – DOI 10.1145/2807591.2807644.

   *Что взято.* Правила, используемые в протоколе:
   - Rule 1 — отношения приводятся вместе с абсолютными значениями базы;
   - Rule 2 — отчитываются все результаты, а не только удачные;
   - Rule 3 — арифметическое среднее только для затрат;
   - Rule 4 — отношения не усредняются, сводятся затраты, а геометрическое среднее допустимо лишь
     как запасной вариант. Отсюда определение ускорения как отношения сумм (§3.7);
   - Rule 5 — для недетерминированных данных приводятся ДИ;
   - Rule 6 — нормальность не предполагается без проверки, используются непараметрические методы;
   - Rule 8 — кроме средних, отчитываются перцентили;
   - Rule 9 — документируются все факторы и установка (`env.json`, §2.7);
   - Rule 11 — сравнение с верхней границей производительности (decode против предела ПСП, §4.7);
   - Rule 12 — распределения на графиках.

   Также взяты требования к таймеру (накладные расходы < 5 % интервала, §3.1), прогрев и разделение
   «тёплого» и «холодного» кэша, минимум n > 5 замеров для непараметрических ДИ.

   *Проверено:* Crossref (DOI, страницы 1–12, подзаголовок); первая страница авторской копии PDF (место,
   даты, ISBN 978-1-4503-3723-6); номер статьи 73 — по карточке ACM Digital Library в результатах
   поиска (сама страница ACM DL для автоматического доступа закрыта); формулировки Rules 1–12 — по тексту PDF.

3. **[Kalibera 2013]** Kalibera, T. Rigorous Benchmarking in Reasonable Time / T. Kalibera, R. Jones. –
   Текст : электронный // ISMM '13 : Proceedings of the 2013 International Symposium on Memory Management
   (Seattle, Washington, USA, June 20–21, 2013). – New York : ACM, 2013. – P. 63–74. –
   DOI 10.1145/2464157.2464160.

   *Что взято.*
   - Многоуровневая модель повторов (сборка → исполнение → итерация) и размерный эксперимент. Число
     повторов на нижнем уровне вычисляется по оценкам дисперсий уровней и стоимостям повторов:
     `r_i = ⌈ sqrt( c_{i+1}·T_i² / (c_i·T_{i+1}²) ) ⌉`, формула (3). Это размерный пилот протокола (§4.9).
   - Результат сообщается как ДИ размера эффекта, то есть отношения времён (Филлер или бутстреп),
     а не как p-значение (§4.2).
   - Проверка независимости итераций по run-sequence и lag-графикам и автокорреляции (§4.7).
   - Иерархическое построение ДИ по уровням: иерархический бутстреп в E2 (§4.1).

   *Проверено:* Crossref (DOI, страницы; та же статья также издана в ACM SIGPLAN Notices, 2013,
   Vol. 48, № 11, DOI 10.1145/2555670.2464160); первая страница копии PDF из Kent Academic Repository
   (место, даты, ISBN 978-1-4503-2100-6); формулы (1)–(5) разделов 9–10.

4. **[Mytkowicz 2009]** Producing Wrong Data Without Doing Anything Obviously Wrong! / T. Mytkowicz,
   A. Diwan, M. Hauswirth, P. F. Sweeney. – Текст : электронный // ASPLOS XIV : Proceedings of the 14th
   International Conference on Architectural Support for Programming Languages and Operating Systems
   (Washington, DC, USA, March 2009). – New York : ACM, 2009. – P. 265–276. – DOI 10.1145/1508244.1508275.

   *Что взято.* Систематическое смещение измерения из-за безобидных на вид факторов установки
   (размер окружения UNIX, порядок линковки). Его не учла ни одна из 133 рассмотренных авторами статей.
   Предложены два средства: рандомизация установки и каузальный анализ. В протоколе это рандомизация и
   балансировка порядка плеч (квадрат Уильямса), фиксация и запись окружения, переменных среды и пути
   запуска (§2.6, §2.7), а также угрозы валидности (§9).

   *Проверено частично:* Crossref и OpenAlex (авторы, DOI, страницы 265–276, ISBN тома
   978-1-60558-406-5); страница публикации у соавтора (USI, sape.inf.usi.ch) — место и месяц. Полный текст
   с бесплатной ссылки ACM для автоматического доступа закрыт, поэтому количественные примеры из статьи
   в протокол не переносились.

5. **[Reddi 2020]** MLPerf Inference Benchmark / V. J. Reddi, C. Cheng, D. Kanter [et al.]. – Текст :
   электронный // 2020 ACM/IEEE 47th Annual International Symposium on Computer Architecture (ISCA)
   (Valencia, Spain, May 30 – June 3, 2020). – IEEE, 2020. – P. 446–459. – DOI 10.1109/ISCA45697.2020.00045.

   *Что взято.* Каждый прогон должен длиться не менее 60 с, чтобы измерялось равновесное поведение
   управления питанием и DVFS, особенно в сценарии single-stream с малым числом запросов. Метрика
   single-stream — 90-й перцентиль задержки (§2.6 п. 6, §3.7). Локальный агент на ноутбуке относится
   к сценарию single-stream.

   *Проверено:* Crossref (DOI, страницы, место и даты конференции); текст препринта arXiv:1911.02549v2
   (раздел III о правилах прогона и сценарии single-stream).

## Б. Статистические процедуры

6. **[Berger 1982]** Berger, R. L. Multiparameter Hypothesis Testing and Acceptance Sampling /
   R. L. Berger. – Текст : электронный // Technometrics. – 1982. – Vol. 24, № 4. – P. 295–300. –
   DOI 10.1080/00401706.1982.10487790.

   *Что взято.* Тест «пересечение — объединение» (intersection–union). Если гипотеза подтверждается
   только при выполнении всех критериев, каждый критерий проверяется на уровне α без поправки, и общий
   уровень остаётся α (§4.6, §4.8).

   *Проверено:* Crossref.

7. **[Card 2020]** With Little Power Comes Great Responsibility / D. Card, P. Henderson, U. Khandelwal
   [et al.]. – Текст : электронный // Proceedings of the 2020 Conference on Empirical Methods in Natural
   Language Processing (EMNLP). – Stroudsburg, PA : Association for Computational Linguistics, 2020. –
   P. 9263–9274. – DOI 10.18653/v1/2020.emnlp-main.745.

   *Что взято.* Мощность сравнений на типичных размерах NLP-наборов часто недостаточна, поэтому её
   нужно оценивать заранее. В протоколе есть таблица мощности TOST (§4.5) и заранее признанная низкая
   мощность H3 на 143 задачах (§6.5).

   *Проверено:* Crossref и OpenAlex.

8. **[Clopper 1934]** Clopper, C. J. The Use of Confidence or Fiducial Limits Illustrated in the Case of
   the Binomial / C. J. Clopper, E. S. Pearson. – Текст : электронный // Biometrika. – 1934. – Vol. 26,
   № 4. – P. 404–413. – DOI 10.1093/biomet/26.4.404.

   *Что взято.* Точный ДИ для доли решённых задач одной конфигурации (§4.3,
   `harness/stats.clopper_pearson`).

   *Проверено:* Crossref.

9. **[Efron 1993]** Efron, B. An Introduction to the Bootstrap / B. Efron, R. J. Tibshirani. – New York :
   Chapman & Hall, 1993. – XVI, 436 p. – ISBN 0-412-04231-2. – Текст : непосредственный.

   *Что взято.* Перцентильный бутстреп-ДИ и проверка гипотез инверсией бутстрепа (§4.1, §4.2).

   *Проверено:* Crossref (запись электронного переиздания, DOI 10.1201/9780429246593); выходные данные
   печатного издания — по рецензии в Psychometrika.

10. **[Holm 1979]** Holm, S. A Simple Sequentially Rejective Multiple Test Procedure / S. Holm. – Текст :
    непосредственный // Scandinavian Journal of Statistics. – 1979. – Vol. 6, № 2. – P. 65–70.

    *Что взято.* Пошаговая поправка Холма для семейств вторичных сравнений (§4.6, `harness/stats.holm`).

    *Проверено:* OpenAlex; титульный лист копии JSTOR (том, номер, страницы, стабильный идентификатор
    JSTOR 4615733). DOI в Crossref не зарегистрирован.

11. **[McNemar 1947]** McNemar, Q. Note on the Sampling Error of the Difference Between Correlated
    Proportions or Percentages / Q. McNemar. – Текст : электронный // Psychometrika. – 1947. – Vol. 12,
    № 2. – P. 153–157. – DOI 10.1007/BF02295996.

    *Что взято.* Сравнение парных бинарных исходов по несовпадающим парам. В протоколе используется
    точный (биномиальный) вариант (§4.4, `harness/stats.mcnemar_exact`).

    *Проверено:* Crossref.

12. **[Schuirmann 1987]** Schuirmann, D. J. A Comparison of the Two One-Sided Tests Procedure and the Power
    Approach for Assessing the Equivalence of Average Bioavailability / D. J. Schuirmann. – Текст :
    электронный // Journal of Pharmacokinetics and Biopharmaceutics. – 1987. – Vol. 15, № 6. –
    P. 657–680. – DOI 10.1007/BF01068419.

    *Что взято.* Процедура двух односторонних тестов (TOST). Эквивалентность принимается, если
    (1 − 2α)-ДИ разности лежит внутри границ ±δ (§4.5).

    *Проверено:* Crossref.

13. **[Tango 1998]** Tango, T. Equivalence Test and Confidence Interval for the Difference in Proportions
    for the Paired-Sample Design / T. Tango. – Текст : электронный // Statistics in Medicine. – 1998. –
    Vol. 17, № 8. – P. 891–908. – DOI 10.1002/(SICI)1097-0258(19980430)17:8<891::AID-SIM780>3.0.CO;2-B.

    *Что взято.* Score-тест и score-ДИ для разности парных долей с ограниченной оценкой максимального
    правдоподобия. Интервал работает и при малом числе несовпадений, где бутстреп и интервал Вальда
    вырождаются. Используется для TOST и проверки «не хуже» (§4.5). Закрытая форма оценки, приведённая
    в протоколе, сверена с численной максимизацией правдоподобия.

    *Проверено:* Crossref.

14. **[Williams 1949]** Williams, E. J. Experimental Designs Balanced for the Estimation of Residual
    Effects of Treatments / E. J. Williams. – Текст : электронный // Australian Journal of Scientific
    Research. Series A: Physical Sciences. – 1949. – Vol. 2, № 2. – P. 149–168. – DOI 10.1071/CH9490149.

    *Что взято.* Латинские квадраты, сбалансированные по эффектам последовательности (каждое условие
    одинаково часто следует за каждым другим). Это порядок плеч внутри раунда (§2.6 п. 3).

    *Проверено:* Crossref.

## В. Данные и программные средства

15. **[Patil 2025]** The Berkeley Function Calling Leaderboard (BFCL) : From Tool Use to Agentic
    Evaluation of Large Language Models / S. G. Patil, H. Mao, F. Yan [et al.]. – Текст : электронный //
    Proceedings of the 42nd International Conference on Machine Learning (ICML 2025). – 2025. –
    (Proceedings of Machine Learning Research ; Vol. 267). – P. 48371–48392. – URL:
    https://proceedings.mlr.press/v267/patil25a.html (дата обращения: 06.10.2026).

    *Что взято.* Бенчмарк, категории, эталонные проверщики и среды многоходовых задач (§2.3, §3.4).
    Данные v4 и проверщики — из репозитория gorilla, коммит 6ea57973c7a6097fd7c5915698c54c17c5b1b6c8.

    *Проверено:* страница статьи в PMLR (авторы, том, страницы).

16. **[llama.cpp]** llama.cpp : LLM inference in C/C++ / ggml-org. – Сборка b11146 (коммит 7fe450e19). –
    Текст : электронный. – URL: https://github.com/ggml-org/llama.cpp (дата обращения: 06.10.2026).

    *Что взято.* Эталон сравнения (llama-server) и вычислительное ядро. Семантика таймеров `prompt_ms` и
    `predicted_ms` сверена по исходному коду сборки (`tools/server/server-context.cpp`, PROTOCOL §3.1),
    перечень методов спекуляции — по `common/speculative.cpp`.

    *Проверено:* исходный код в `third_party/llama.cpp` на указанном коммите.

## Соответствие разделов протокола и источников

| Раздел PROTOCOL.md | Источники |
|---|---|
| §0.3 общие запреты | Georges 2007; Hoefler 2015 (Rules 2–4, 6) |
| §2.5 гигиена, приложение C | Georges 2007 (состояние, переживающее инвокацию); Hoefler 2015 (тёплый и холодный кэш) |
| §2.6 порядок, блоки, прогрев | Georges 2007; Kalibera 2013; Mytkowicz 2009; Williams 1949; Reddi 2020 |
| §2.7 запись окружения | Hoefler 2015 (Rule 9); Mytkowicz 2009 |
| §3 метрики | Hoefler 2015 (Rules 1, 3, 4, 8); Reddi 2020 (p90) |
| §4.1–4.2 бутстреп, отношения | Efron 1993; Kalibera 2013 |
| §4.3–4.5 точность, McNemar, TOST | Clopper 1934; McNemar 1947; Schuirmann 1987; Tango 1998; Card 2020 |
| §4.6 множественность | Holm 1979; Berger 1982 |
| §4.7 диагностика | Kalibera 2013; Hoefler 2015 (Rules 11–12) |
| §4.9 размерный пилот | Kalibera 2013 |
| §2.3, §3.4 нагрузка и точность | Patil 2025 |
