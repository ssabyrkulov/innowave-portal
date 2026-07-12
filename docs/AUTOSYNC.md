# 🔄 Автозагрузка выгрузок 1С из Google Drive

Схема: 1С кладёт Excel в папку Drive (уже работает) → Google Apps Script
раз в 15 минут отправляет новые/изменённые файлы на портал → портал сам
определяет тип файла по колонкам и импортирует (дубли отсекаются).

Настройка занимает ~5 минут и делается один раз.

## Шаг 1. Взять токен автоприёма

Render → сервис **innowave-portal** → **Environment** → значение
переменной `INBOX_TOKEN` (нажмите на глаз, скопируйте).

Если переменной нет — добавьте: **Add Environment Variable**,
Key `INBOX_TOKEN`, Value — любая длинная случайная строка (40+ символов).

## Шаг 2. Создать Apps Script

1. Откройте [script.google.com](https://script.google.com) под тем же
   Google-аккаунтом, где лежит папка выгрузок → **Новый проект**.
2. Сотрите заготовку и вставьте код ниже.
3. Впишите свой `TOKEN` из шага 1 (FOLDER_ID уже указан верно —
   это ваша папка выгрузок).

```javascript
// ===== Настройки =====
const FOLDER_ID = '1HBmmRwmxPnxkbtr_x4bkKnQdc1F9wNhD'; // папка выгрузок 1С
const ENDPOINT  = 'https://innowave-group.com/integrations/inbox';
const TOKEN     = 'ВСТАВЬТЕ_INBOX_TOKEN_СЮДА';
// =====================

function syncNewFiles() {
  const props = PropertiesService.getScriptProperties();
  const files = DriveApp.getFolderById(FOLDER_ID).getFiles();

  while (files.hasNext()) {
    const f = files.next();
    if (!/\.(xlsx|xlsm)$/i.test(f.getName())) continue;

    const key = 'sent_' + f.getId();
    const mod = String(f.getLastUpdated().getTime());
    if (props.getProperty(key) === mod) continue; // не менялся — пропускаем

    try {
      const res = UrlFetchApp.fetch(ENDPOINT, {
        method: 'post',
        headers: { Authorization: 'Bearer ' + TOKEN },
        payload: { file: f.getBlob() },
        muteHttpExceptions: true,
      });
      const code = res.getResponseCode();
      if (code === 200) {
        props.setProperty(key, mod); // запомнили — файл доставлен
        console.log(f.getName() + ' -> ' + res.getContentText());
      } else {
        console.warn(f.getName() + ' -> HTTP ' + code + ': ' + res.getContentText());
        // не помечаем как отправленный — попробуем в следующий запуск
      }
    } catch (e) {
      console.error(f.getName() + ' -> ' + e); // сервер спал/сеть — повторим позже
    }
  }
}
```

4. Сохраните (Ctrl+S), назовите проект, например `innowave-portal-sync`.

## Шаг 3. Проверить вручную

Вверху выберите функцию `syncNewFiles` → **Выполнить**. Google попросит
разрешения (доступ к Drive и внешним адресам) — разрешите.
В журнале выполнения должны появиться строки вида
`ВыгрузкаРеал2.xlsx -> {"type":"sales","status":"imported",...}`.

На портале: раздел **Контроль → Журнал загрузок** — появятся записи
«[авто] …» от пользователя «Автозагрузка (Drive)».

## Шаг 4. Включить расписание

Слева иконка ⏰ **Триггеры** → **Добавить триггер**:
- Функция: `syncNewFiles`
- Источник: **Триггер по времени**
- Тип: **Таймер с интервалом в минутах** → **каждые 15 минут**
- Сохранить.

Готово: дальше всё едет само. Первый запрос после «сна» бесплатного
Render может не успеть (таймаут) — скрипт просто доставит файл со
следующей попытки через 15 минут.

## Как это устроено со стороны портала

- `POST /integrations/inbox`, авторизация — `Bearer INBOX_TOKEN`
- Тип файла определяется по заголовкам колонок:
  продажи (есть «НоменклатураНаименование»), поступления денег
  (Дата/Сумма/Контрагент/ВидОперации). Неопознанные форматы фиксируются
  в журнале со статусом «не распознан» — под них добавляются импортёры.
- Повторная отправка того же файла (по SHA-256 содержимого) — no-op;
  изменённый файл дозаливает только новые строки (построчная
  дедупликация).
