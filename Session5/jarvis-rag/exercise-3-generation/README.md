# תרגיל 3 — מתוצאות חיפוש לתשובה עם מקורות

בתרגיל2 קיבלתם קטעים מדורגים. כאן אתם בונים את הקוד שבוחר אילו קטעים ייכנסו לבקשה למודל, מצרף את השאלה המקורית, ומציג תשובה מול המקורות שעליהם היא נשענת. התרחיש הוא אירוע התחברות סינתטי: מה צריך לבדוק קודם, ומה עדיין חסר כדי לקבוע סיבה?

התוצר הוא `artifacts/answer.json` ולצדו `artifacts/answer.html`. בדף תראו כל טענה, את מזהי המקורות ואת הטקסט שנשלח בפועל. אתם קוראים את הטקסט ובודקים אם הוא באמת תומך בטענה. מזהה תקין לבדו אינו הוכחה.

## 1. מה כל קובץ עושה

- `student.py` — הקובץ שאתם עורכים. בליבה משלימים `assemble_context`. בבונוס1 משלימים `invalid_citation_ids`.
- `run.py` — נקודת ההפעלה. בוחרת מסלול, טוענת קלט ושומרת תשובה ודף צפייה.
- `runtime.py` — תשתית מסופקת: HTTP, הוראות למודל, סכמת פלט, בדיקות מבנה ותצוגה. אין צורך לשנות אותה לליבה.
- `check_exercise.py` — בדיקות מקומיות ו־HTTP mocks. אינו מפעיל מודל.
- `fixtures/search-results.json` — קטעי מקור אמיתיים מתוך חומרי התרחיש הסינתטי, שנבחרו ידנית לצורך הרצה עצמאית. הדירוג והציונים מומצאים; הם אינם פלט embeddings או מדד לאיכות Retrieval.
- `fixtures/answer.json` — תשובה שנכתבה מראש כדי לבדוק תשתית ותצוגה ללא מודל.

החבילה אינה כוללת פתרון. אין צורך לקרוא את כל מסמכי הפרויקט מראש: קטעי המקור כבר נמצאים בקלט. התשובה הקבועה מתייחסת רק ל־fixture המצורף, ואין להשתמש בה להערכת שאלה אחרת או תוצאות חיפוש אחרות.

## 2. הכנה ופתיחת התיקייה

נדרשת Python3.10 ומעלה. כל הקוד משתמש בספרייה המובנית: אין פקודת pip install ואין צורך ב־SDK. חלצו את ה־ZIP לתיקיית `exercise-3-generation` ופתחו אותה ב־VS Code באמצעות File → Open Folder. פתחו Terminal → New Terminal.

ב־macOS/Linux, מתוך תיקיית Session4 שבה חלצתם:

```sh
cd exercise-3-generation
python3 --version
python3 run.py --help
```

ב־Windows PowerShell, מאותה תיקיית Session4:

```powershell
cd exercise-3-generation
py -3 --version
py -3 run.py --help
```

הפקודות בהמשך כתובות עם `python3`. ב־Windows החליפו אותו ב־`py -3`. אם אתם משתמשים בסביבה וירטואלית שכבר הופעלה, אפשר להשתמש ב־`python`. אין צורך לפתוח טרמינל נפרד לכל קובץ.

## 3. מתחילים בכשל מכוון

```sh
python3 run.py --provider offline_fixture
python3 check_exercise.py
```

ההרצה הראשונה מציגה:

```text
ERROR: TODO: implement assemble_context in student.py
```

גם בדיקות הרכבת ההקשר ייכשלו עד שתשלימו את הפונקציה. זהו מצב הפתיחה של המשימה, לא כשל התקנה. אל תחליפו את קובץ הבדיקות כדי לגרום לו לעבור.

## 4. מה לבנות בליבה

פתחו את `student.py`. הפונקציה `assemble_context(question, results, max_chars)` מחזירה מילון עם `question` ו־`sources`. כל מקור ברשימת `sources` כולל `chunk_id`, `text` ו־`metadata` כפי שהגיעו מהחיפוש.

בחרו קטעים לפי `rank` מהקטן לגדול; אל תכניסו אותו מזהה פעמיים. צברו את מספר התווים של **טקסט המקורות בלבד**, והוסיפו רק קטעים שלמים שנכנסים בתקציב. אם קטע גדול מדי, אפשר להמשיך ולבדוק קטע קצר יותר שבא אחריו. אל תחתכו את המקור ואל תשכתבו אותו. אם לא נבחר שום מקור, העלו `ValueError` עם הסבר.

`max_chars` הוא תקציב לימודי בתווים. בבקשה האמיתית נשלחים גם הוראות מערכת והשאלה; זו אינה מדידת טוקנים או הוכחה שהבקשה מתאימה לחלון ההקשר של כל מודל.

אפשר להיעזר ב־Claude Code בבקשה כזו:

> הסבר לי את חוזה assemble_context ואת בדיקות הליבה. עזור לי לממש בחירת קטעים שלמים לפי rank, בלי כפילויות ובלי שינוי טקסט. השאר את פונקציית הבונוס כ־TODO. הסבר כל שורה חדשה בעברית ואל תשנה את הבדיקות או את runtime.py.

אחרי המימוש:

```sh
python3 check_exercise.py
python3 run.py --provider offline_fixture
```

הצפי: עשר בדיקות עוברות (`Ran 10 tests ... OK`), ובפלט ההרצה:

```text
provider=offline_fixture citation_status=citations_not_validated
sources=2 seconds=<measured time>
saved=artifacts/answer.json viewer=artifacts/answer.html
```

הזמן נמדד בכל הרצה ואינו מספר קבוע. הסטטוס אומר שבודק ההפניות האוטומטי עדיין לא מומש. פתחו את `artifacts/answer.html` בדפדפן באמצעות File → Open, או לחצו על הקובץ ב־VS Code. קראו את הטענות מול הקטעים ונסחו מה חסר. הפלט הקבוע רק מאמת שהקבצים, הפענוח והתצוגה עובדים; הוא **אינו מדגים תגובה של LLM להקשר**.

## 5. חיבור ל־Generation Model מקומי — Ollama

זו הרצה אמיתית שיש לבצע לאחר בדיקת הליבה, עם המרצה. היא טרם נבדקה בחומרת הכיתה. נדרש Ollama פועל ומודל יצירת טקסט שכבר הורד. המודל ששימש ל־embeddings בתרגילים1–2 אינו בהכרח מודל שמייצר תשובות.

פתחו את יישום Ollama. בדקו מה כבר מותקן:

```sh
ollama list
```

בחרו שם מדויק של דגם יצירת טקסט מהרשימה. אם אין כזה, אל תורידו דגם במהלך התרגיל בלי תיאום; השתמשו במסלול הענן עם גישה קיימת או המתינו למרצה. גודל קטן, למשל סדר גודל3B–4B, עשוי להתאים לחלק מהמחשבים אך אינו מבטיח התאמה לזיכרון, איכות בעברית או JSON תקין.

ב־macOS/Linux הגדירו את השם שבחרתם במקום הטקסט שבסוגריים:

```sh
export GENERATION_MODEL="<exact installed text model name>"
python3 run.py --input ../exercise-2-retrieval/build/search-results.json --provider ollama --model "$GENERATION_MODEL" --output artifacts/local-answer.json
```

ב־Windows PowerShell:

```powershell
$env:GENERATION_MODEL="<exact installed text model name>"
py -3 run.py --input ../exercise-2-retrieval/build/search-results.json --provider ollama --model $env:GENERATION_MODEL --output artifacts/local-answer.json
```

אם תוצאות תרגיל2 אינן זמינות, אפשר להשתמש בקלט האצור. זה עדיין LLM אמיתי, אך שלב החיפוש עצמו יהיה fixture:

```sh
python3 run.py --provider ollama --model "$GENERATION_MODEL" --output artifacts/local-fixture-input.json
```

ב־Windows השתמשו ב־`py -3` וב־`$env:GENERATION_MODEL`. פתחו את דף ה־HTML שנוצר. ודאו שב־JSON נשמר `provider=ollama`; השדה `retrieval_mode` מספר אם המקורות הגיעו מחיפוש אמיתי או מ־fixture. אין דרישה שהניסוח יהיה זהה לתשובה הקבועה.

## 6. מסלול ענן — Anthropic Messages API (Claude)

נדרשים מפתח API עם חיוב וגישה לדגם התומך Structured Outputs. מנוי לצ'אט אינו מבטיח גישה ל־API. הקוד שולח לספק את השאלה ואת המקורות שנבחרו. בחומר זה כל המסמכים סינתטיים.

שם הדגם ניתן לשינוי באמצעות `--model`. `claude-opus-5-5` מופיע בתיעוד הרשמי של Anthropic; הזמינות בחשבון שלכם עדיין דורשת בדיקה. זו דוגמה להגדרה, לא טענה שהרצה זו כבר נבדקה או ששולמה.

ב־macOS/Linux, כדי להזין מפתח בלי לשמור אותו בשורת הפקודה:

```sh
read -s ANTHROPIC_API_KEY
export ANTHROPIC_API_KEY
export GENERATION_MODEL="claude-opus-5-5"
python3 run.py --input ../exercise-2-retrieval/build/search-results.json --provider anthropic --model "$GENERATION_MODEL" --output artifacts/cloud-answer.json
```

אחרי `read` הדביקו את המפתח ולחצו Enter; אין ציפייה שיופיע על המסך. אל תעתיקו את המפתח לקוד, ל־JSON או ל־ZIP.

ב־Windows PowerShell:

```powershell
$credential = Read-Host "Anthropic API key" -AsSecureString
$env:ANTHROPIC_API_KEY = [System.Net.NetworkCredential]::new("", $credential).Password
$env:GENERATION_MODEL="claude-opus-5-5"
py -3 run.py --input ../exercise-2-retrieval/build/search-results.json --provider anthropic --model $env:GENERATION_MODEL --output artifacts/cloud-answer.json
```

אם אין מפתח או הרשאה, אפשר לעבוד על הליבה והבדיקות ללא רשת. ה־fixture אינו תחליף לבדיקת Generation אמיתית; את הבדיקה הזו עושים עם המרצה בהמשך.

הפלט כולל `provider=anthropic`, מספר מקורות, זמן ונתיבי JSON/HTML. יש לבדוק כל טענה מול המקור גם אם המבנה תקין. `seconds` כולל המתנה לספק ופענוח, ואינו זמן המודל בלבד.

## 7. בונוס1 — בודק הפניות אוטומטי

השלימו את `invalid_citation_ids(answer, sources)` ב־`student.py`. בנו קבוצה של המזהים **שנשלחו בפועל**, עברו על ההפניות בכל טענה והחזירו מזהים שאינם בקבוצה, ללא כפילויות ובסדר ממוין. מקור שקיים באינדקס אך לא נכנס לבקשה אינו מותר לציטוט.

```sh
python3 check_exercise.py --bonus
python3 run.py --provider offline_fixture --require-citations
```

הצפי:12 בדיקות עוברות, והסטטוס הוא `source_ids_valid_not_semantic_proof`. הוסיפו מזהה מומצא לעותק של `fixtures/answer.json`, הריצו עם `--fixture` שמצביע לעותק, ובדקו שהדו״ח מסמן `invalid_source_ids` ושהתהליך מחזיר קוד2. אין צורך בשינוי המקור המקורי.

למשל, אחרי ששמרתם עותק בשם `fixtures/answer-invalid.json` ושיניתם בו הפניה:

```sh
python3 run.py --provider offline_fixture --fixture fixtures/answer-invalid.json --require-citations --output artifacts/invalid-answer.json
```

פתחו את ה־HTML וראו `SOURCE NOT SENT TO MODEL`. לאחר מכן הוסיפו לעותק **טענה שאינה נתמכת** אך עם מזהה חוקי. הבודק עשוי לעבור: זו ההמחשה לכך שבדיקת מזהים אינה בדיקת תמיכה סמנטית. סמנו את הטענה השגויה בבדיקה האנושית.

## 8. בונוס2 — שתי תצורות הקשר: איכות וזמן

השוו שתי תצורות על אותן שאלות ובאותו Generation Model. בתצורהA שמרו את התקציב הרגיל. בתצורהB שנו רק את תקציב המקורות או את כלל בחירת הקטעים ב־`assemble_context`. שמרו תוצאות בשמות נפרדים ופתחו את שני דפי ה־HTML.

דוגמה למסלול מקומי, לאחר הגדרת GENERATION_MODEL:

```sh
python3 run.py --input ../exercise-2-retrieval/build/search-results.json --provider ollama --model "$GENERATION_MODEL" --max-chars 12000 --output artifacts/context-a.json
python3 run.py --input ../exercise-2-retrieval/build/search-results.json --provider ollama --model "$GENERATION_MODEL" --max-chars 4000 --output artifacts/context-b.json
```

ב־Windows החליפו את הפעלת Python ואת משתנה הסביבה כפי שהוסבר. לענן החליפו רק `--provider ollama` ב־`--provider anthropic` ובחרו שם דגם ענן נגיש. השתמשו באותו ספק בשתי התצורות. אם התקציבים בחרו בדיוק אותם קטעים, זו אינה השוואה משמעותית: פתחו את `context.sources` והתאימו את התקציב לגדלי הקטעים בפועל. אם אין מקום לשום מקור, צפויה הודעת שגיאה ולא תשובה חלופית.

קראו לפחות שלוש שאלות מתוצאות תרגיל2. לכל תצורה בדקו: האם ניתנה הנחיה ראשונה שימושית, האם הטענות נתמכות בגרסה הנכונה, האם נשמרה ההבחנה בין תצפית להשערה, והאם המידע החסר צוין. השוו גם `seconds`. כדי לצמצם השפעת טעינת מודל ותקשורת, הריצו כמה חזרות בסדר מתחלף; אין להסיק מהרצה אחת שמודל או תצורה מהירים יותר. החזרות בענן הן קריאות API נוספות.

אין להשוות איכות או latency בעזרת `offline_fixture`: הטקסט בו קבוע ואינו מושפע מההקשר. אין צורך בדו״ח להגשה; השוו את התוצרים ב־VS Code ובדפדפן והכינו דוגמת כשל אחת לדיון.

## 9. זמן עבודה מוצע

40 דקות לליבה:5 דקות לקריאת חוזה הקלט והמקורות;15 דקות לבניית Context Assembly בעזרת Claude Code;10 דקות להרצה ולתיקון;10 דקות לקריאת טענה מול מקור ולשינוי אחד מנומק. זו הערכת תכנון שטרם נמדדה עם הכיתה. הבונוסים מיועדים למי שסיים ואינם תנאי להשלמת הליבה. הכנת חשבונות, הורדת מודלים ופתרון התקנה עשויים לדרוש זמן נוסף ואינם מוסווים כחלק מזמן המימוש.

## 10. הודעות נפוצות ומה עושים

- `TODO: implement assemble_context` — פתחו `student.py` והשלימו את ליבת המשימה.
- `No complete source fits max_chars` — הגדילו את התקציב או בדקו את גדלי הקטעים; אל תחתכו ראיה באמצע.
- `No evidence: search results are empty` — חזרו ל־Retrieval. אין כאן תשובה מבוססת מקורות.
- `Set ANTHROPIC_API_KEY` — הגדירו מפתח בסביבה לפי סעיף6, או בחרו מסלול ללא רשת.
- `HTTP 401/403` — בדקו מפתח והרשאות. `HTTP404` יכול להעיד על מודל או כתובת שגויים. אין להדפיס מפתח כדי לאבחן זאת.
- `Connection failed` — בדקו שהשרת פועל ושהכתובת נכונה. ל־Ollama מקומי ברירת המחדל היא `http://localhost:11434`.
- JSON לא תקין, refusal או תשובה חלקית — ההרצה נעצרת. בדקו גישה, דגם מתאים, אורך פלט והנחיות. אל תציגו פלט חלקי כהצלחה.
- `citations_not_validated` — הליבה רצה, בונוס1 עדיין לא מומש. בדקו מקורות ידנית.
- `invalid_source_ids` — התשובה נשמרת לצורך אבחון, אך יש הפניות למקורות שלא נשלחו.

## מה אומת בחבילה

נבדקו תחביר, הכשל המכוון ב־starter, בדיקות עם פתרון המרצה בעותק פרטי, יצירת JSON/HTML ומעטפות Anthropic/Ollama באמצעות HTTP mocks. לא בוצעה קריאת API בתשלום ולא הורץ LLM מקומי בשלב הכנת החבילה. בדיקת מסלולי local/cloud אמיתיים נשארת לבדיקה משותפת עם המרצה.

## מקורות לתשתית

- [Anthropic Structured Outputs — Messages API](https://docs.claude.com/en/docs/build-with-claude/structured-outputs): סכמת הפלט המבוקש. סכמת JSON אינה הוכחה לנכונות תוכן.
- [Anthropic — Claude models overview](https://docs.claude.com/en/docs/about-claude/models/overview): שם דגם לדוגמת ההגדרה; גישה בפועל תלויה בחשבון.
- [Ollama Chat API](https://docs.ollama.com/api/chat): מסלול Native API, השדות messages/format/stream ותשובת message.content.

התיעוד נבדק ב־4 בנובמבר2026. המסמכים בתרחיש וה־fixture הם חומר כיתתי סינתטי של הקורס.
