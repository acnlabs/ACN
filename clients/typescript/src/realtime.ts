/**
 * ACN WebSocket Client
 * 
 * Real-time communication with ACN server.
 */

import type { WSConnectionOptions, WSMessage } from './types';

/** WebSocket event handler */
export type WSEventHandler<T = unknown> = (message: WSMessage<T>) => void;

/** WebSocket state */
export type WSState = 'connecting' | 'connected' | 'disconnected' | 'reconnecting';

/**
 * ACN Real-time Client
 * 
 * @example
 * ```typescript
 * import { ACNRealtime } from '@acn/client';
 * 
 * const realtime = new ACNRealtime('ws://localhost:9000');
 * 
 * // Subscribe to a channel
 * realtime.subscribe('agents', (message) => {
 *   console.log('Agent event:', message);
 * });
 * 
 * // Connect
 * await realtime.connect();
 * ```
 */
export class ACNRealtime {
  private baseUrl: string;
  private options: Required<WSConnectionOptions>;
  private ws: WebSocket | null = null;
  private state: WSState = 'disconnected';
  private reconnectAttempts = 0;
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  
  private channels: Map<string, Set<WSEventHandler>> = new Map();
  private globalHandlers: Set<WSEventHandler> = new Set();
  private stateHandlers: Set<(state: WSState) => void> = new Set();

  constructor(baseUrl: string, options?: WSConnectionOptions) {
    // Convert http(s) to ws(s)
    this.baseUrl = baseUrl
      .replace(/^http:/, 'ws:')
      .replace(/^https:/, 'wss:')
      .replace(/\/$/, '');
    
    this.options = {
      autoReconnect: options?.autoReconnect ?? true,
      reconnectInterval: options?.reconnectInterval ?? 3000,
      maxReconnectAttempts: options?.maxReconnectAttempts ?? 10,
      heartbeatInterval: options?.heartbeatInterval ?? 30000,
    };
  }

  /** Current connection state */
  get connectionState(): WSState {
    return this.state;
  }

  /** Whether currently connected */
  get isConnected(): boolean {
    return this.state === 'connected';
  }

  /**
   * Connect to a channel
   */
  async connect(channel = 'default'): Promise<void> {
    if (this.ws && this.state === 'connected') {
      return;
    }

    return new Promise((resolve, reject) => {
      this.setState('connecting');
      
      try {
        this.ws = new WebSocket(`${this.baseUrl}/ws/${channel}`);
        
        this.ws.onopen = () => {
          this.setState('connected');
          this.reconnectAttempts = 0;
          this.startHeartbeat();
          resolve();
        };

        this.ws.onclose = (event) => {
          this.stopHeartbeat();
          
          if (event.wasClean) {
            this.setState('disconnected');
          } else if (this.options.autoReconnect && this.reconnectAttempts < this.options.maxReconnectAttempts) {
            this.scheduleReconnect(channel);
          } else {
            this.setState('disconnected');
          }
        };

        this.ws.onerror = (error) => {
          if (this.state === 'connecting') {
            reject(new Error('WebSocket connection failed'));
          }
          this.handleError(error);
        };

        this.ws.onmessage = (event) => {
          this.handleMessage(event.data);
        };
      } catch (error) {
        this.setState('disconnected');
        reject(error);
      }
    });
  }

  /**
   * Disconnect from server
   */
  disconnect(): void {
    this.stopHeartbeat();
    this.clearReconnectTimer();
    
    if (this.ws) {
      this.ws.close(1000, 'Client disconnect');
      this.ws = null;
    }
    
    this.setState('disconnected');
  }

  /**
   * Subscribe to a channel
   */
  subscribe<T = unknown>(channel: string, handler: WSEventHandler<T>): () => void {
    let handlers = this.channels.get(channel);
    if (!handlers) {
      handlers = new Set();
      this.channels.set(channel, handlers);
    }
    handlers.add(handler as WSEventHandler);

    // Return unsubscribe function
    return () => {
      handlers?.delete(handler as WSEventHandler);
      if (handlers?.size === 0) {
        this.channels.delete(channel);
      }
    };
  }

  /**
   * Subscribe to all messages
   */
  onMessage<T = unknown>(handler: WSEventHandler<T>): () => void {
    this.globalHandlers.add(handler as WSEventHandler);
    return () => {
      this.globalHandlers.delete(handler as WSEventHandler);
    };
  }

  /**
   * Subscribe to state changes
   */
  onStateChange(handler: (state: WSState) => void): () => void {
    this.stateHandlers.add(handler);
    return () => {
      this.stateHandlers.delete(handler);
    };
  }

  /**
   * Send a message
   */
  send(message: unknown): void {
    if (!this.ws || this.state !== 'connected') {
      throw new Error('WebSocket not connected');
    }
    this.ws.send(JSON.stringify(message));
  }

  // ============================================
  // Private Methods
  // ============================================

  private setState(state: WSState): void {
    this.state = state;
    this.stateHandlers.forEach((handler) => handler(state));
  }

  private handleMessage(data: string): void {
    try {
      const message: WSMessage = JSON.parse(data);
      
      // Notify global handlers
      this.globalHandlers.forEach((handler) => handler(message));
      
      // Notify channel-specific handlers
      const channelHandlers = this.channels.get(message.channel);
      if (channelHandlers) {
        channelHandlers.forEach((handler) => handler(message));
      }

      // Also notify handlers subscribed to the message type
      const typeHandlers = this.channels.get(message.type);
      if (typeHandlers) {
        typeHandlers.forEach((handler) => handler(message));
      }
    } catch (error) {
      console.error('Failed to parse WebSocket message:', error);
    }
  }

  private handleError(error: Event): void {
    console.error('WebSocket error:', error);
  }

  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.heartbeatTimer = setInterval(() => {
      if (this.ws && this.state === 'connected') {
        this.send({ type: 'ping', timestamp: new Date().toISOString() });
      }
    }, this.options.heartbeatInterval);
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }

  private scheduleReconnect(channel: string): void {
    this.clearReconnectTimer();
    this.setState('reconnecting');
    this.reconnectAttempts++;
    
    const delay = this.options.reconnectInterval * Math.min(this.reconnectAttempts, 5);
    
    this.reconnectTimer = setTimeout(() => {
      this.connect(channel).catch(() => {
        // Reconnect failed, will try again if attempts remaining
      });
    }, delay);
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }
}

/**
 * Create a simple subscription to ACN events
 * 
 * @example
 * ```typescript
 * const unsubscribe = subscribeToACN('ws://localhost:9000', 'agents', (msg) => {
 *   console.log('Agent event:', msg);
 * });
 * 
 * // Later: unsubscribe();
 * ```
 */
export function subscribeToACN<T = unknown>(
  baseUrl: string,
  channel: string,
  handler: WSEventHandler<T>
): () => void {
  const realtime = new ACNRealtime(baseUrl);
  const unsubscribe = realtime.subscribe(channel, handler);
  
  realtime.connect(channel).catch((error) => {
    console.error('Failed to connect to ACN:', error);
  });

  return () => {
    unsubscribe();
    realtime.disconnect();
  };
}

