# Clip Agent — مراقب البث واللقطات

ترسل رابط بث مباشر (تويتش أو يوتيوب) ← البرنامج يراقب ← عند انفجار الشات أو ارتفاع الصوت يحفظ لقطة عمودية 9:16 ويرفعها مسودة في Rofilm Hub. أنت تضغط نشر.

## الإعداد (مرة واحدة)
1. أنشئ حساب GitHub مجاني، ثم مستودع جديد **Public** باسم clip-agent وارفع إليه ملفات هذا المجلد.
2. في المستودع: Settings ← Secrets and variables ← Actions ← New repository secret:
   - HUB_BASE = رابط موقع Rofilm Hub (بدون / في النهاية)
   - HUB_TOKEN = مفتاح الـ API من صفحة إعدادات Rofilm Hub
3. حدّث Rofilm Hub بالتعديل الجديد (ملف app.py فيه new_token) ثم Push.

## التشغيل كل مرة
Actions ← Watch stream ← Run workflow ← الصق رابط البث ← Run.
يراقب حتى 5 ساعات ويقدر الجهاز مغلق.
