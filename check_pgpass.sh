#!/bin/bash

echo "=== Проверка настройки .pgpass ==="
echo ""

# 1. Проверка существования файла
if [ -f ~/.pgpass ]; then
    echo "✅ Файл .pgpass существует"
else
    echo "❌ Файл .pgpass не найден!"
    echo ""
    echo "Создайте файл:"
    echo "cat > ~/.pgpass << EOF"
    echo "10.8.0.1:5432:fox_crypto_new:elcrypto:ваш_пароль_здесь"
    echo "EOF"
    echo "chmod 600 ~/.pgpass"
    exit 1
fi

# 2. Проверка прав доступа
PERMS=$(stat -c %a ~/.pgpass 2>/dev/null || stat -f %A ~/.pgpass 2>/dev/null)
if [ "$PERMS" = "600" ]; then
    echo "✅ Права доступа корректные (600)"
else
    echo "❌ Неправильные права доступа: $PERMS"
    echo "Исправьте командой: chmod 600 ~/.pgpass"
fi

# 3. Проверка содержимого
echo ""
echo "Содержимое .pgpass (пароль скрыт):"
while IFS=: read -r host port db user pass; do
    echo "  Host: $host, Port: $port, DB: $db, User: $user, Pass: ***"
done < ~/.pgpass

# 4. Проверка подключения
echo ""
echo "Проверка подключения к базе данных..."
if psql -h 10.8.0.1 -U elcrypto -d fox_crypto_new -c "SELECT version();" > /dev/null 2>&1; then
    echo "✅ Подключение успешно!"
else
    echo "❌ Не удается подключиться!"
    echo ""
    echo "Возможные причины:"
    echo "1. Неправильный пароль в .pgpass"
    echo "2. VPN не подключен"
    echo "3. PostgreSQL не доступен"
    echo ""
    echo "Попробуйте подключиться вручную:"
    echo "psql -h 10.8.0.1 -U elcrypto -d fox_crypto_new"
fi