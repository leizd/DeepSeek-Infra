package com.deepseek.mobile;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.SocketException;

import javax.net.ServerSocketFactory;

/** Acquire streams before publishing an accepted socket to asynchronous gRPC readers. */
final class PlatformServerSocketFactory extends ServerSocketFactory {
    @Override public ServerSocket createServerSocket() throws IOException { return new AcceptedStreamsServerSocket(); }
    @Override public ServerSocket createServerSocket(int port) throws IOException { return createServerSocket(port, 50); }
    @Override public ServerSocket createServerSocket(int port, int backlog) throws IOException {
        return createServerSocket(port, backlog, InetAddress.getByName("127.0.0.1"));
    }
    @Override public ServerSocket createServerSocket(int port, int backlog, InetAddress address) throws IOException {
        if (address == null || !address.isLoopbackAddress()) throw new SocketException("Platform RPC requires loopback");
        ServerSocket server = createServerSocket();
        try {
            server.bind(new InetSocketAddress(address, port), backlog);
            return server;
        } catch (IOException | RuntimeException failure) {
            try { server.close(); } catch (IOException closeFailure) { failure.addSuppressed(closeFailure); }
            throw failure;
        }
    }

    private static final class AcceptedStreamsServerSocket extends ServerSocket {
        AcceptedStreamsServerSocket() throws IOException { super(); }

        @Override public Socket accept() throws IOException {
            if (isClosed()) throw new SocketException("Socket is closed");
            if (!isBound()) throw new SocketException("Socket is not bound");
            AcceptedStreamsSocket socket = new AcceptedStreamsSocket();
            try {
                implAccept(socket);
                socket.prepareStreams();
                return socket;
            } catch (IOException | RuntimeException failure) {
                try { socket.close(); } catch (IOException closeFailure) { failure.addSuppressed(closeFailure); }
                throw failure;
            }
        }
    }

    private static final class AcceptedStreamsSocket extends Socket {
        private InputStream input;
        private OutputStream output;

        void prepareStreams() throws IOException {
            // No other thread can close this socket yet. Android 7's privileged
            // stream accessor can throw a null exception when close races it.
            input = super.getInputStream();
            output = super.getOutputStream();
        }

        @Override public InputStream getInputStream() throws IOException {
            if (isClosed()) throw new SocketException("Socket is closed");
            if (isInputShutdown()) throw new SocketException("Socket input is shutdown");
            if (input == null) throw new SocketException("Accepted stream is not ready");
            return input;
        }

        @Override public OutputStream getOutputStream() throws IOException {
            if (isClosed()) throw new SocketException("Socket is closed");
            if (isOutputShutdown()) throw new SocketException("Socket output is shutdown");
            if (output == null) throw new SocketException("Accepted stream is not ready");
            return output;
        }
    }
}
