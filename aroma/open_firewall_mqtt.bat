@echo off
chcp 65001 >nul
echo ========================================
echo  放行 Mosquitto MQTT 端口 1883（需管理员）
echo ========================================
echo.

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 请右键本文件 -^>「以管理员身份运行」
    pause
    exit /b 1
)

netsh advfirewall firewall delete rule name="Mosquitto MQTT 1883 Inbound" >nul 2>&1
netsh advfirewall firewall add rule name="Mosquitto MQTT 1883 Inbound" dir=in action=allow protocol=TCP localport=1883 profile=private,domain,public

if %errorlevel% equ 0 (
    echo [成功] 已添加防火墙入站规则: TCP 1883
    echo.
    echo 请重启 ESP32，串口应出现:
    echo   [网络] TCP 端口可达
    echo   [MQTT] 成功连接 MQTT Broker
) else (
    echo [失败] 添加规则失败，错误码 %errorlevel%
)

echo.
pause
