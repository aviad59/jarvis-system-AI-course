import argparse  # קוראים את מצב ההפעלה ואת פרטי המודל המקומי
import json  # מתעדים הודעות, קריאות כלי ותוצאות בפורמט שאפשר לבדוק
import os  # מעבירים לתהליך ה־CLI את סביבת העבודה המקומית
from pathlib import Path  # שומרים תוצרים רק בתיקיית הבנייה של התרגיל
import shutil  # מאתרים את פקודת ה־CLI שהוכנה מראש
import subprocess  # מפעילים פקודות מוגדרות באמצעות רשימת ארגומנטים
import tempfile  # מפרידים בין תוצרי הרצות שונות
import urllib.request  # פונים לשרת Ollama המקומי באמצעות HTTP
HERE = Path(__file__).resolve().parent  # מבססים את הנתיבים על מיקום קובץ התרגיל
TOOL = {'type': 'function', 'function': {'name': 'create_connection_document', 'description': 'Create a short test document with LibreOffice CLI and return real ODT/PDF paths. Use the supplied title and paragraphs without adding facts.', 'parameters': {'type': 'object', 'properties': {'title': {'type': 'string'}, 'paragraphs': {'type': 'array', 'items': {'type': 'string'}}}, 'required': ['title', 'paragraphs']}}}  # מתארים פעולה עסקית צרה שהמודל יכול לבקש

def execute_cli(binary, arguments):  # מרכזים הפעלה ישירה של פקודה מתוך רשימה ולא מחרוזת מעטפת
    result = subprocess.run([binary, '--json'] + arguments, capture_output=True, text=True, timeout=90, env=os.environ.copy())  # מגבילים את זמן הפעולה ושומרים את הפלט לבדיקה
    if result.returncode:  # כשל של היישום אינו הופך להצלחה של המודל
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or 'CLI command failed')  # מחזירים מידע אבחוני מהפקודה שנכשלה
    return json.loads(result.stdout)  # תוצאת JSON מאפשרת להעביר למודל נתונים במקום הודעת הצלחה כללית

def create_document(binary, title, paragraphs):  # מממשים את הכלי שהמודל רואה באמצעות מספר פקודות CLI קבועות
    if not isinstance(title, str) or not title.strip() or len(title) > 120:  # בודקים כותרת לפני פתיחת תהליך חיצוני
        raise ValueError('title must contain 1..120 characters')  # מאפשרים לתלמיד לאבחן קלט שאינו מתאים להגדרה
    if not isinstance(paragraphs, list) or not 1 <= len(paragraphs) <= 5 or any(not isinstance(p, str) or len(p) > 2000 for p in paragraphs):  # שומרים על קלט מצומצם למסמך הבדיקה
        raise ValueError('paragraphs must contain 1..5 short strings')  # מפרידים תקלה בקלט מתקלה ביישום
    build = HERE / 'build'  # מגדירים תחום קבוע לתוצרי התרגול
    build.mkdir(exist_ok=True)  # מכינים את תיקיית הבנייה בהרצה הראשונה
    run_dir = Path(tempfile.mkdtemp(prefix='connection-', dir=build))  # כל בדיקה מקבלת תיקייה חדשה ואינה דורסת מסמך קודם
    project = run_dir / 'document.json'  # קובץ הפרויקט הוא מצב העבודה של ה־CLI ולא מסמך המקור של ה־RAG
    commands = []  # שומרים את הפקודות ואת התוצאות כהוכחת ביצוע
    args = ['document', 'new', '--type', 'writer', '--name', title, '-o', str(project)]  # יוצרים מסמך Writer חדש
    commands.append({'args': args, 'result': execute_cli(binary, args)})  # מבצעים את היצירה ושומרים את הפלט האמיתי
    args = ['--project', str(project), 'writer', 'add-heading', '-t', title, '--level', '1']  # מוסיפים כותרת מתוך הקלט שנבדק
    commands.append({'args': args, 'result': execute_cli(binary, args)})  # מתעדים את הוספת הכותרת
    for paragraph in paragraphs:  # מעבירים כל פסקה כערך נתונים ולא כפקודת מעטפת
        args = ['--project', str(project), 'writer', 'add-paragraph', '-t', paragraph]  # בוחרים רק את פעולת הוספת הפסקה שהוגדרה בכלי
        commands.append({'args': args, 'result': execute_cli(binary, args)})  # שומרים את תוצאת פעולת העריכה
    for extension in ['odt', 'pdf']:  # מפיקים מסמך מקורי וגם PDF שמציג את תוצאת העבודה
        args = ['--project', str(project), 'export', 'render', str(run_dir / ('connection.' + extension)), '--preset', extension]  # פורמט PDF מפעיל את LibreOffice המקומי
        commands.append({'args': args, 'result': execute_cli(binary, args)})  # מסיימים רק אם פקודת ההפקה הצליחה
    files = [run_dir / 'connection.odt', run_dir / 'connection.pdf']  # מגדירים את שני התוצרים שהתרגיל דורש
    if not all(p.is_file() and p.stat().st_size > 0 for p in files):  # מאמתים שקיימים קבצים ולא מסתפקים בטקסט שהמודל כתב
        raise RuntimeError('Expected real ODT and PDF outputs')  # מסמנים בדיקת תוצאה שנכשלה
    return {'files': [str(p) for p in files], 'commands': commands, 'verified_files_exist': True}  # מחזירים נתונים שאפשר להציג ולפתוח

def chat(base_url, payload):  # מפרידים את בקשת המודל מהפעלת הכלי בקוד
    data = json.dumps(payload).encode()  # מקודדים הודעת JSON לשירות המקומי
    request = urllib.request.Request(base_url.rstrip('/') + '/api/chat', data=data, headers={'Content-Type': 'application/json'})  # משתמשים ב־API המתועד של Ollama
    with urllib.request.urlopen(request, timeout=180) as response:  # מגבילים זמן ומסיימים את החיבור בסוף הקריאה
        return json.load(response)['message']  # מחזירים את הודעת המודל עם בקשת הכלי או התשובה

def run(args):  # אותו מימוש CLI משמש לבדיקה ישירה ולבדיקת בחירת מודל
    binary = str(args.cli) if args.cli else shutil.which('cli-anything-libreoffice')  # מאפשרים לבחור את הפקודה שהותקנה בסביבת התרגיל
    if not binary:  # עוצרים לפני בקשת מודל כשהיישום לא הוכן
        raise ValueError('Install the pinned LibreOffice CLI first or pass --cli')  # מונעים ניסוי שמסתיר תלות חסרה
    title = 'Cedar connection check'  # זו בדיקת חיבור מצומצמת ולא תדריך הסיום של היום
    paragraphs = ['This is a local CLI connection test. No task board was changed.']  # נתוני הבדיקה אינם כוללים מידע ארגוני
    trace = {'mode': args.mode, 'model': args.model if args.mode == 'ollama' else None, 'calls': [], 'quality': 'inspect actual files and content'}  # לא מתייחסים לסיום לולאה כציון איכות
    if args.mode == 'direct':  # בודקים תחילה את ה־CLI ללא בחירת מודל
        trace['calls'].append(create_document(binary, title, paragraphs))  # מפעילים את הכלי עם קלט ידוע
    else:  # המודל המקומי בוחר כלי ומחזיר ארגומנטים
        messages = [{'role': 'user', 'content': 'Use the provided tool once to create a test document. Title: ' + title + '. One paragraph exactly: ' + paragraphs[0]}]  # מגדירים בקשה קבועה שאפשר להשוות לתוצר
        payload = {'model': args.model, 'messages': messages, 'tools': [TOOL], 'stream': False, 'think': False, 'options': {'temperature': 0, 'num_predict': 800}}  # בוחרים פלט תחום ומודל שכבר הותקן
        message = chat(args.base_url, payload)  # שומרים את בחירת המודל לפני כל ביצוע
        trace['model_message'] = message  # מאפשרים לבדוק מה המודל הציע בפועל
        calls = message.get('tool_calls', [])  # תשובה טקסטואלית לבדה אינה יוצרת מסמך
        if len(calls) != 1 or calls[0]['function']['name'] != TOOL['function']['name']:  # דורשים את פעולת הבדיקה שהגדרנו בדיוק פעם אחת
            raise ValueError('Expected one create_connection_document tool call')  # אין מעבר סמוי למסלול ישיר כשהמודל לא בחר כלי
        inputs = calls[0]['function']['arguments']  # מפרידים את הקלט שהציע המודל מן הבקשה המקורית
        result = create_document(binary, **inputs)  # הקוד הוא שמפעיל את ה־CLI, לא Ollama עצמו
        trace['calls'].append({'arguments': inputs, 'result': result})  # מחברים בחירת כלי, ביצוע וקבצים בתיעוד אחד
        messages += [message, {'role': 'tool', 'tool_name': TOOL['function']['name'], 'content': json.dumps(result, ensure_ascii=False)}]  # מחזירים למודל את התוצאה שהתקבלה מהיישום
        payload.update(messages=messages, tools=[])  # מבקשים סיכום לאחר שהמסמך כבר נוצר
        trace['final_message'] = chat(args.base_url, payload)  # תשובת הסיום היא תיאור הביצוע ולא ההוכחה היחידה לו
    args.output.parent.mkdir(parents=True, exist_ok=True)  # מכינים נתיב תיעוד מקומי
    args.output.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding='utf-8')  # שומרים את ראיות הביצוע ואת הקלטים
    print(json.dumps({'trace': str(args.output), 'calls': len(trace['calls'])}))  # מציגים סיכום קצר שאפשר לזהות במסוף

if __name__ == '__main__':  # טעינת הקובץ אינה מפעילה מודל או יוצרת מסמך
    parser = argparse.ArgumentParser()  # מציגים אפשרויות הפעלה ברורות
    parser.add_argument('--mode', choices=['direct', 'ollama'], default='direct')  # מתחילים מבדיקה ישירה ורק אז בודקים מודל
    parser.add_argument('--cli', type=Path)  # מאפשרים נתיב מדויק של סביבת התרגיל
    parser.add_argument('--model', default='qwen3:8b')  # בוחרים מועמד מקומי שנבדק בנפרד
    parser.add_argument('--base-url', default='http://127.0.0.1:11434')  # ברירת המחדל פונה למחשב המקומי
    parser.add_argument('--output', type=Path, default=HERE / 'build/cli-trace.json')  # התיעוד נשמר לצד תוצרי התרגיל
    run(parser.parse_args())  # מפעילים את המסלול שהמשתמש בחר
