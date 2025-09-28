#!/usr/bin/env python3
"""
ДИАГНОСТИКА ПРОБЛЕМЫ БЕЗ ИЗМЕНЕНИЯ КОДА
Проблема найдена: строка 214 в celery_efficiency_parallel.py
"""

print("="*60)
print("АНАЛИЗ ПРОБЛЕМЫ")
print("="*60)
print()

print("📍 МЕСТО ЗАВИСАНИЯ:")
print("   Файл: celery_efficiency_parallel.py")
print("   Строка: 214")
print("   Код: for i, result in enumerate(job.results):")
print()

print("🔴 ПРОБЛЕМА:")
print("   При большом количестве комбинаций (729) происходит:")
print("   1. Создается 729 subtasks в группе")
print("   2. apply_async() запускает их асинхронно")
print("   3. Сразу же начинается итерация по job.results")
print("   4. НО! job.results может быть еще не инициализирован")
print("   5. Или итерация блокируется при попытке получить список результатов")
print()

print("📊 ПОЧЕМУ РАБОТАЕТ С МАЛЫМ КОЛИЧЕСТВОМ:")
print("   - 9-25 комбинаций: результаты успевают инициализироваться")
print("   - 729 комбинаций: слишком много, инициализация зависает")
print()

print("🔧 РЕШЕНИЕ БЕЗ ИЗМЕНЕНИЯ ЛОГИКИ:")
print()
print("ВАРИАНТ 1: Проверка готовности группы перед итерацией")
print("-"*50)
print("""
# Вместо сразу:
for i, result in enumerate(job.results):

# Сначала проверить:
if not hasattr(job, 'results') or job.results is None:
    time.sleep(1)
    continue

# Или безопаснее - получить результаты через AsyncResult:
for subtask_id in job.subtasks:
    result = AsyncResult(subtask_id)
    if result.ready():
        ...
""")
print()

print("ВАРИАНТ 2: Разбиение на батчи")
print("-"*50)
print("""
# Для 729 комбинаций - обрабатывать порциями:
if total_combinations > 500:
    # Разбить на супер-батчи по 100-200
    for super_batch in chunks(combinations, 200):
        job = group(subtasks_for_batch).apply_async()
        # Ждать завершения супер-батча
        results.extend(job.get(timeout=600))
""")
print()

print("ВАРИАНТ 3: Использовать правильную проверку результатов")
print("-"*50)
print("""
# Вместо итерации по job.results:
while completed < total_combinations:
    # Проверяем состояние группы
    if job.ready():
        # Группа завершена - собираем результаты
        all_results = job.join(timeout=60)
        break

    # Или проверяем отдельные задачи
    completed_count = sum(1 for r in job.children if r.ready())

    time.sleep(2)
""")
print()

print("⚠️  КРИТИЧНО:")
print("   НЕ МЕНЯЙ основную логику обработки!")
print("   Добавь только безопасную проверку перед строкой 214")
print()

print("✅ МИНИМАЛЬНОЕ ИСПРАВЛЕНИЕ:")
print("-"*50)
print("""
# Добавить перед строкой 214:

# Ждем инициализации результатов (максимум 10 секунд)
init_timeout = 10
init_start = time.time()
while time.time() - init_start < init_timeout:
    if hasattr(job, 'results') and job.results:
        break
    time.sleep(0.5)
    logger.debug(f"Waiting for job.results initialization...")

if not hasattr(job, 'results') or not job.results:
    logger.error("job.results not initialized after {init_timeout}s")
    # Fallback на другой метод получения результатов
""")