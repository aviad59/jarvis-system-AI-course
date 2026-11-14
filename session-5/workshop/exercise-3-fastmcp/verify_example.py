import asyncio  # מריצים גילוי וקריאת כלים דרך הלקוח האסינכרוני
import sys  # מפעילים את השרת באמצעות אותו Python שבו מותקנת FastMCP
from pathlib import Path  # מוצאים את קובץ הדוגמה בלי תלות בתיקייה שממנה הופעלה הבדיקה
from fastmcp import Client  # משתמשים בלקוח של החבילה העצמאית לבדיקה דרך הפרוטוקול
from fastmcp.client.transports import StdioTransport  # מחברים תהליך שרת אמיתי דרך קלט ופלט במקום קריאה ישירה לפונקציה
async def main():  # מרכזים את בדיקות הקטלוג, הקלט והתוצאה בתרחיש מנותק אחד
    server = Path(__file__).with_name("example_server.py")  # בוחרים את דוגמת המרצה הזעירה ולא שרת חיצוני
    transport = StdioTransport(command=sys.executable, args=[str(server)])  # מגדירים תהליך מקומי בסביבת FastMCP החדשה
    async with Client(transport) as client:  # פותחים חיבור MCP שייסגר גם אם בדיקה נכשלת
        tools = await client.list_tools()  # קוראים את הקטלוג שהשרת באמת מפרסם
        assert [tool.name for tool in tools] == ["validate_task_brief"]  # דורשים שרק כלי הדוגמה ייחשף
        schema = tools[0].input_schema  # קוראים את הסכמה שנוצרה מחתימת Python
        assert set(schema["required"]) == {"title", "goal"}  # דורשים ששני השדות מוגדרים כחובה
        assert schema["properties"]["title"]["type"] == "string"  # מוודאים שכותרת הוגדרה כטקסט ולא כשדה חופשי
        valid = await client.call_tool("validate_task_brief", {"title": "הכנת פגישה", "goal": "לזהות מידע חסר"})  # מפעילים בקשה תקינה דרך MCP
        assert valid.data["valid"] is True and valid.data["missing_fields"] == []  # בודקים תוצאה מובנית של הצלחה ולא רק הודעת טקסט
        blank = await client.call_tool("validate_task_brief", {"title": " ", "goal": "מטרה"})  # מבדילים בין טקסט תקין מבחינת סוג לבין תוכן חסר מבחינה עסקית
        assert blank.data["valid"] is False and blank.data["missing_fields"] == ["title"]  # דורשים שהכלל העסקי יצביע על השדה החסר
        missing = await client.call_tool("validate_task_brief", {"title": "כותרת"}, raise_on_error=False)  # שולחים בקשה שחסר בה שדה חובה בסכמה
        assert missing.is_error  # מצפים לשגיאת קלט במקום להפעלה מוצלחת
        wrong_type = await client.call_tool("validate_task_brief", {"title": ["אינו טקסט"], "goal": "מטרה"}, raise_on_error=False)  # שולחים סוג ערך שגוי כדי לבדוק את הסכמה המופקת
        assert wrong_type.is_error  # מוודאים שהבקשה השגויה לא הפכה לתוצאת הצלחה
        print("PASS: actual stdio catalogue, required-field schema, structured valid result, blank-field rule, missing field and invalid type")  # מדווחים בדיוק מה נבדק ללא מודל או שירות חיצוני
if __name__ == "__main__":  # מריצים בדיקות רק בהפעלה ישירה של הקובץ
    asyncio.run(main())  # פותחים לולאת אירועים לצורך חיבור MCP ובדיקותיו
