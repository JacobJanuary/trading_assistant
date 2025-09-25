// SSE Manager - надежный менеджер для Server-Sent Events соединений
class SSEManager {
    constructor(url, options = {}) {
        this.url = url;
        this.options = {
            reconnectDelay: options.reconnectDelay || 3000,
            maxReconnectAttempts: options.maxReconnectAttempts || 5,
            heartbeatTimeout: options.heartbeatTimeout || 10000,
            debug: options.debug || false
        };
        
        this.eventSource = null;
        this.reconnectAttempts = 0;
        this.isConnecting = false;
        this.isClosed = false;
        this.lastActivity = Date.now();
        this.heartbeatTimer = null;
        this.callbacks = {};
    }
    
    log(message) {
        if (this.options.debug) {
            console.log(`[SSE Manager] ${message}`);
        }
    }
    
    connect() {
        if (this.isConnecting || this.eventSource) {
            this.log('Already connected or connecting');
            return;
        }
        
        this.isConnecting = true;
        this.isClosed = false;
        this.log(`Connecting to ${this.url}`);
        
        try {
            this.eventSource = new EventSource(this.url);
            
            // Успешное открытие соединения
            this.eventSource.onopen = () => {
                this.log('Connection opened');
                this.isConnecting = false;
                this.reconnectAttempts = 0;
                this.lastActivity = Date.now();
                this.startHeartbeatMonitor();
                
                if (this.callbacks.onOpen) {
                    this.callbacks.onOpen();
                }
            };
            
            // Получение сообщения
            this.eventSource.onmessage = (event) => {
                this.lastActivity = Date.now();
                
                try {
                    const data = JSON.parse(event.data);
                    this.log(`Received: ${data.type}`);
                    
                    if (this.callbacks.onMessage) {
                        this.callbacks.onMessage(data);
                    }
                } catch (error) {
                    this.log(`Error parsing message: ${error}`);
                }
            };
            
            // Обработка ошибок
            this.eventSource.onerror = (error) => {
                this.log(`Connection error: ${error}`);
                this.isConnecting = false;
                
                // Закрываем текущее соединение
                if (this.eventSource) {
                    this.eventSource.close();
                    this.eventSource = null;
                }
                
                this.stopHeartbeatMonitor();
                
                // Пытаемся переподключиться, если не закрыто намеренно
                if (!this.isClosed && this.reconnectAttempts < this.options.maxReconnectAttempts) {
                    this.reconnectAttempts++;
                    this.log(`Reconnecting in ${this.options.reconnectDelay}ms (attempt ${this.reconnectAttempts}/${this.options.maxReconnectAttempts})`);
                    
                    if (this.callbacks.onReconnecting) {
                        this.callbacks.onReconnecting(this.reconnectAttempts);
                    }
                    
                    setTimeout(() => {
                        if (!this.isClosed) {
                            this.connect();
                        }
                    }, this.options.reconnectDelay);
                } else if (!this.isClosed) {
                    this.log('Max reconnection attempts reached');
                    if (this.callbacks.onError) {
                        this.callbacks.onError('Max reconnection attempts reached');
                    }
                }
            };
            
        } catch (error) {
            this.log(`Failed to create EventSource: ${error}`);
            this.isConnecting = false;
            
            if (this.callbacks.onError) {
                this.callbacks.onError(error);
            }
        }
    }
    
    startHeartbeatMonitor() {
        this.stopHeartbeatMonitor();
        
        this.heartbeatTimer = setInterval(() => {
            const timeSinceLastActivity = Date.now() - this.lastActivity;
            
            if (timeSinceLastActivity > this.options.heartbeatTimeout) {
                this.log(`No activity for ${timeSinceLastActivity}ms, reconnecting...`);
                
                // Принудительно закрываем и переподключаемся
                if (this.eventSource) {
                    this.eventSource.close();
                    this.eventSource = null;
                }
                
                this.connect();
            }
        }, 5000); // Проверяем каждые 5 секунд
    }
    
    stopHeartbeatMonitor() {
        if (this.heartbeatTimer) {
            clearInterval(this.heartbeatTimer);
            this.heartbeatTimer = null;
        }
    }
    
    close() {
        this.log('Closing connection');
        this.isClosed = true;
        this.stopHeartbeatMonitor();
        
        if (this.eventSource) {
            this.eventSource.close();
            this.eventSource = null;
        }
        
        this.isConnecting = false;
    }
    
    on(event, callback) {
        this.callbacks[event] = callback;
    }
}

// Экспортируем для использования
if (typeof module !== 'undefined' && module.exports) {
    module.exports = SSEManager;
}