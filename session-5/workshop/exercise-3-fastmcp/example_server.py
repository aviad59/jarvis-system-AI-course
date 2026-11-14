# דוגמה זעירה בלבד: הצוותים בוחרים מקרה שימוש וכלים משלהם בתרגיל.
# זיכרון שיחה: ה־Host בוחר איזה הקשר להעביר למודל; הפונקציה אינה מקבלת שיחות קודמות אוטומטית.
# נתוני יישום: כאן אין אחסון בין קריאות. שמירת נתונים דורשת קובץ או מסד נתונים מפורש.
# MCP: מעביר גילוי כלים, קלט ותוצאה; הוא אינו זיכרון שיחה או מאגר הנתונים של היישום.
from fastmcp import FastMCP  # משתמשים בחבילת FastMCP העצמאית, לא במחלקה מתוך MCP SDK
mcp = FastMCP("task-brief-validator")  # נותנים שם לשרת הבדיקה שיופיע בזמן החיבור
@mcp.tool(description="Check required task brief fields and return missing fields.")  # מפרסמים פעולה קטנה שהמודל יכול להבין לפני בקשת הפעלה
def validate_task_brief(title: str, goal: str) -> dict:  # החתימה יוצרת סכמת קלט עם שני שדות טקסט נדרשים
    brief = {"title": title.strip(), "goal": goal.strip()}  # מנקים רווחים בלי לשנות את המשמעות שהמשתמש מסר
    missing = [field for field, value in brief.items() if not value]  # מזהים גם שדה שהכיל רווחים בלבד
    return {"valid": not missing, "missing_fields": missing, "brief": brief}  # מחזירים תוצאה מובנית שניתן לבדוק במקום תשובת הצלחה כללית
if __name__ == "__main__":  # מונעים הפעלת שרת אוטומטית כשמייבאים את הקובץ לבדיקה
    mcp.run(transport="stdio")  # ממתינים ללקוח MCP שמפעיל את השרת כתהליך מקומי
