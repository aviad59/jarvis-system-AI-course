import argparse  # בוחרים את קובץ השרת ואת פעולת הבדיקה בלי לשנות את קוד הלקוח
import asyncio  # מנהלים חיבור MCP אסינכרוני שייסגר בסיום
import json  # קוראים קלט מקובץ ושומרים פלט מובנה לבדיקה
import sys  # מפעילים את השרת באותה סביבת Python שבה מותקנת FastMCP
from pathlib import Path  # פותרים נתיבי שרת וקלט מקומיים
from fastmcp import Client  # מפעילים לקוח MCP אמיתי ולא קוראים ישירות לפונקציה
from fastmcp.client.transports import StdioTransport  # מפעילים את קובץ השרת כתהליך מקומי
async def inspect(args):  # גילוי וביצוע עוברים באותו חיבור לשרת שנבחר
    transport = StdioTransport(command=sys.executable, args=[str(args.server.resolve())])  # מגדירים פקודת הרצה מפורשת לקובץ התרגיל
    async with Client(transport) as client:  # סוגרים את התהליך והחיבור גם במקרה כשל
        tools = await client.list_tools()  # קוראים את ההגדרות שהשרת באמת מפרסם
        catalogue = [{"name": tool.name, "description": tool.description, "input_schema": tool.input_schema} for tool in tools]  # מציגים את השם, התיאור והקלט של כל כלי
        print(json.dumps({"tools": catalogue}, ensure_ascii=False, indent=2))  # מאפשרים להשוות את הקטלוג לדרישות הצוות
        if args.tool:  # קריאת כלי מתבצעת רק כשביקשו שם כלי מפורש
            if not args.arguments_file:  # מונעים ניחוש קלט שהמשתמש לא סיפק
                raise ValueError("--arguments-file is required with --tool")  # מציינים מה חסר להפעלה
            arguments = json.loads(args.arguments_file.read_text(encoding="utf-8"))  # קוראים JSON מקובץ כדי להימנע מבעיות מרכאות במסוף
            if not isinstance(arguments, dict):  # קלט כלי צריך להיות אובייקט שדות ולא רשימה
                raise ValueError("Tool arguments must be a JSON object")  # עוצרים לפני שליחת בקשה שאינה במבנה הנדרש
            result = await client.call_tool(args.tool, arguments, raise_on_error=False)  # מחזירים גם תוצאת כשל גלויה לבדיקה
            print(json.dumps({"is_error": result.is_error, "data": result.data, "content": [item.model_dump(mode="json") for item in result.content]}, ensure_ascii=False, indent=2))  # מציגים ראיית ביצוע ולא טענה על הצלחת המודל
if __name__ == "__main__":  # ייבוא הקובץ אינו מפעיל שרת או כלי
    parser = argparse.ArgumentParser()  # מספקים עזרה לאפשרויות ההרצה
    parser.add_argument("--server", type=Path, default=Path("starter.py"))  # השרת שבנה הצוות הוא ברירת המחדל
    parser.add_argument("--tool")  # ללא שם כלי מבוצע גילוי בלבד
    parser.add_argument("--arguments-file", type=Path)  # כל מקרה בדיקה נשמר בקובץ שהצוות יכול לקרוא
    asyncio.run(inspect(parser.parse_args()))  # מפעילים את החיבור לפי האפשרויות שנבחרו
