"""בודק הכנות לפני הכיתה. הקריאה למודל embeddings מתבצעת רק עם --sample.

במסלול ענן, בדיקת משתנה סביבה בלבד אינה מוכיחה שיש הרשאת API או תקציב.
"""
import argparse  # טוענים כלי עבור קריאת אפשרויות משורת הפקודה
import getpass  # טוענים כלי עבור הקלדת מפתח בהסתרה
import os  # טוענים כלי עבור קריאת משתני הסביבה בלי לשמור מפתח בקובץ
import sys  # טוענים כלי עבור דיווח קוד סיום והודעות שגיאה
from index_support import resolve_model, embed_texts, embed_voyage, validate_vectors  # טוענים את פונקציות העזר הדרושות לשלב הנוכחי


def main():  # מגדירים אפשרויות הרצה ומפעילים את המסלול שהלומד בחר
    parser = argparse.ArgumentParser()  # מכינים את קורא אפשרויות ההרצה
    parser.add_argument("--provider", choices=("ollama", "voyage"), default="ollama")  # מגדירים אפשרות הרצה: --provider
    parser.add_argument("--model")  # מגדירים אפשרות הרצה: --model
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")  # מגדירים אפשרות הרצה: --base-url
    parser.add_argument("--sample", action="store_true", help="Actually embed one short example; cloud API may charge")  # מגדירים אפשרות הרצה: --sample
    parser.add_argument("--ask-key", action="store_true", help="Read cloud key privately if not set")  # מגדירים אפשרות הרצה: --ask-key
    args = parser.parse_args()  # קוראים את אפשרויות ההרצה שנבחרו
    try:  # מפעילים את השלב עם טיפול מרוכז בשגיאות צפויות
        # בודקים את מסלול ההכנה שנבחר; מפתח API נשאר בזיכרון הסביבה בלבד.
        model = args.model or ("embeddinggemma" if args.provider == "ollama" else "voyage-3.5")  # קובעים את מודל ה־embeddings המתאים למסלול
        if args.provider == "voyage" and args.ask_key and not os.environ.get("VOYAGE_API_KEY"):  # בוחרים את מסלול החישוב המתאים לספק שנשמר או נבחר
            os.environ["VOYAGE_API_KEY"] = getpass.getpass("Voyage AI API key (hidden): ")  # משלימים את ביטוי הנתונים של main תוך שמירה על המבנה המוצג
        if args.provider == "ollama":  # בוחרים את מסלול החישוב המתאים לספק שנשמר או נבחר
            model, _ = resolve_model(args.base_url, model)  # משלימים את ביטוי הנתונים של main תוך שמירה על המבנה המוצג
            print(f"LOCAL MODEL READY: {model}")  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה
        elif not os.environ.get("VOYAGE_API_KEY", "").strip():  # מבקשים מפתח בסתר רק כאשר נבחר ענן ואין מפתח זמין בסביבה
            raise ValueError("VOYAGE_API_KEY is missing. See cloud setup in README-HE.html")  # עוצרים עם הסבר ברור כאשר החוזה או הקלט אינם תקינים
        else:  # מפעילים את המסלול החלופי עבור מצב הקלט הזה
            print("CLOUD KEY PRESENT: key value is hidden; access/billing not verified")  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה
        if args.sample:  # מחשבים embedding אמיתי לדוגמה רק כאשר נבחרה אפשרות sample
            # זו בקשת embedding אמיתית. במסלול Voyage AI היא עשויה להיות בתשלום.
            texts = ["title: Atlas Demo | text: בדיקת התחברות למערכת"] if args.provider == "ollama" else ["בדיקת התחברות למערכת"]  # אוספים את טקסטי הקלט למודל לפי סדר הרשומות
            vectors = embed_texts(args.base_url, model, texts) if args.provider == "ollama" else embed_voyage(model, texts, "document")  # אוספים את הווקטורים שהתקבלו עבור הקלטים
            print("REAL EMBEDDING VERIFIED; DIMENSIONS:", validate_vectors(vectors))  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה
    except ValueError as error:  # מתרגמים שגיאה צפויה להודעה שמסבירה מה צריך לתקן
        print(f"STOP: {error}", file=sys.stderr)  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה
        return 2  # מחזירים קוד כשל כדי שהטרמינל יציג שהשלב נעצר
    return 0  # מסמנים שהפעולה הסתיימה בהצלחה


if __name__ == "__main__":  # מפעילים את התוכנית כאשר מריצים את הקובץ עצמו
    raise SystemExit(main())  # מסיימים את התוכנית עם קוד הסיום שהחזירה פונקציית ההפעלה
