"""בודק הכנות לפני הכיתה. הקריאה למודל embeddings מתבצעת רק עם --sample.
"""
import argparse  # טוענים כלי עבור קריאת אפשרויות משורת הפקודה
import sys  # טוענים כלי עבור דיווח קוד סיום והודעות שגיאה
from index_support import resolve_model, embed_texts, validate_vectors  # טוענים את פונקציות העזר הדרושות לשלב הנוכחי


def main():  # מגדירים אפשרויות הרצה ומפעילים את המסלול שהלומד בחר
    parser = argparse.ArgumentParser()  # מכינים את קורא אפשרויות ההרצה
    parser.add_argument("--provider", choices=("ollama",), default="ollama")  # מגדירים אפשרות הרצה: --provider
    parser.add_argument("--model")  # מגדירים אפשרות הרצה: --model
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")  # מגדירים אפשרות הרצה: --base-url
    parser.add_argument("--sample", action="store_true", help="Actually embed one short example")  # מגדירים אפשרות הרצה: --sample
    args = parser.parse_args()  # קוראים את אפשרויות ההרצה שנבחרו
    try:  # מפעילים את השלב עם טיפול מרוכז בשגיאות צפויות
        model = args.model or "embeddinggemma"  # קובעים את מודל ה־embeddings
        model, _ = resolve_model(args.base_url, model)  # בודקים שהמודל מותקן ב־Ollama
        print(f"LOCAL MODEL READY: {model}")  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה
        if args.sample:  # מחשבים embedding אמיתי לדוגמה רק כאשר נבחרה אפשרות sample
            texts = ["title: Atlas Demo | text: בדיקת התחברות למערכת"]  # אוספים את טקסטי הקלט למודל
            vectors = embed_texts(args.base_url, model, texts)  # אוספים את הווקטורים שהתקבלו עבור הקלטים
            print("REAL EMBEDDING VERIFIED; DIMENSIONS:", validate_vectors(vectors))  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה
    except ValueError as error:  # מתרגמים שגיאה צפויה להודעה שמסבירה מה צריך לתקן
        print(f"STOP: {error}", file=sys.stderr)  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה
        return 2  # מחזירים קוד כשל כדי שהטרמינל יציג שהשלב נעצר
    return 0  # מסמנים שהפעולה הסתיימה בהצלחה


if __name__ == "__main__":  # מפעילים את התוכנית כאשר מריצים את הקובץ עצמו
    raise SystemExit(main())  # מסיימים את התוכנית עם קוד הסיום שהחזירה פונקציית ההפעלה
