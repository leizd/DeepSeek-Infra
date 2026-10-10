package com.deepseek.mobile.platform.v1;

import static io.grpc.MethodDescriptor.generateFullMethodName;

/**
 * <pre>
 * Read-only platform decoding and bundled ML Kit inference. No product paths,
 * credentials, business state, or authority private keys cross this boundary.
 * The Android launcher supplies a per-process random credential over authenticated
 * loopback gRPC. Uploads begin with exactly one header, followed by &lt;=64 KiB data
 * frames. Half-close commits only an exact length and SHA-256 match. The service
 * uses private temporary files and deletes them on completion/cancellation.
 * </pre>
 */
@io.grpc.stub.annotations.GrpcGenerated
public final class PlatformOcrEngineGrpc {

  private PlatformOcrEngineGrpc() {}

  public static final java.lang.String SERVICE_NAME = "deepseek.platform.v1.PlatformOcrEngine";

  // Static method descriptors that strictly reflect the proto.
  private static volatile io.grpc.MethodDescriptor<com.deepseek.mobile.platform.v1.PlatformOcr.DocumentChunk,
      com.deepseek.mobile.platform.v1.PlatformOcr.RecognizedDocument> getRecognizeImageMethod;

  @io.grpc.stub.annotations.RpcMethod(
      fullMethodName = SERVICE_NAME + '/' + "RecognizeImage",
      requestType = com.deepseek.mobile.platform.v1.PlatformOcr.DocumentChunk.class,
      responseType = com.deepseek.mobile.platform.v1.PlatformOcr.RecognizedDocument.class,
      methodType = io.grpc.MethodDescriptor.MethodType.CLIENT_STREAMING)
  public static io.grpc.MethodDescriptor<com.deepseek.mobile.platform.v1.PlatformOcr.DocumentChunk,
      com.deepseek.mobile.platform.v1.PlatformOcr.RecognizedDocument> getRecognizeImageMethod() {
    io.grpc.MethodDescriptor<com.deepseek.mobile.platform.v1.PlatformOcr.DocumentChunk, com.deepseek.mobile.platform.v1.PlatformOcr.RecognizedDocument> getRecognizeImageMethod;
    if ((getRecognizeImageMethod = PlatformOcrEngineGrpc.getRecognizeImageMethod) == null) {
      synchronized (PlatformOcrEngineGrpc.class) {
        if ((getRecognizeImageMethod = PlatformOcrEngineGrpc.getRecognizeImageMethod) == null) {
          PlatformOcrEngineGrpc.getRecognizeImageMethod = getRecognizeImageMethod =
              io.grpc.MethodDescriptor.<com.deepseek.mobile.platform.v1.PlatformOcr.DocumentChunk, com.deepseek.mobile.platform.v1.PlatformOcr.RecognizedDocument>newBuilder()
              .setType(io.grpc.MethodDescriptor.MethodType.CLIENT_STREAMING)
              .setFullMethodName(generateFullMethodName(SERVICE_NAME, "RecognizeImage"))
              .setSampledToLocalTracing(true)
              .setRequestMarshaller(io.grpc.protobuf.lite.ProtoLiteUtils.marshaller(
                  com.deepseek.mobile.platform.v1.PlatformOcr.DocumentChunk.getDefaultInstance()))
              .setResponseMarshaller(io.grpc.protobuf.lite.ProtoLiteUtils.marshaller(
                  com.deepseek.mobile.platform.v1.PlatformOcr.RecognizedDocument.getDefaultInstance()))
              .build();
        }
      }
    }
    return getRecognizeImageMethod;
  }

  private static volatile io.grpc.MethodDescriptor<com.deepseek.mobile.platform.v1.PlatformOcr.DocumentChunk,
      com.deepseek.mobile.platform.v1.PlatformOcr.RecognizedDocument> getRecognizePdfMethod;

  @io.grpc.stub.annotations.RpcMethod(
      fullMethodName = SERVICE_NAME + '/' + "RecognizePdf",
      requestType = com.deepseek.mobile.platform.v1.PlatformOcr.DocumentChunk.class,
      responseType = com.deepseek.mobile.platform.v1.PlatformOcr.RecognizedDocument.class,
      methodType = io.grpc.MethodDescriptor.MethodType.CLIENT_STREAMING)
  public static io.grpc.MethodDescriptor<com.deepseek.mobile.platform.v1.PlatformOcr.DocumentChunk,
      com.deepseek.mobile.platform.v1.PlatformOcr.RecognizedDocument> getRecognizePdfMethod() {
    io.grpc.MethodDescriptor<com.deepseek.mobile.platform.v1.PlatformOcr.DocumentChunk, com.deepseek.mobile.platform.v1.PlatformOcr.RecognizedDocument> getRecognizePdfMethod;
    if ((getRecognizePdfMethod = PlatformOcrEngineGrpc.getRecognizePdfMethod) == null) {
      synchronized (PlatformOcrEngineGrpc.class) {
        if ((getRecognizePdfMethod = PlatformOcrEngineGrpc.getRecognizePdfMethod) == null) {
          PlatformOcrEngineGrpc.getRecognizePdfMethod = getRecognizePdfMethod =
              io.grpc.MethodDescriptor.<com.deepseek.mobile.platform.v1.PlatformOcr.DocumentChunk, com.deepseek.mobile.platform.v1.PlatformOcr.RecognizedDocument>newBuilder()
              .setType(io.grpc.MethodDescriptor.MethodType.CLIENT_STREAMING)
              .setFullMethodName(generateFullMethodName(SERVICE_NAME, "RecognizePdf"))
              .setSampledToLocalTracing(true)
              .setRequestMarshaller(io.grpc.protobuf.lite.ProtoLiteUtils.marshaller(
                  com.deepseek.mobile.platform.v1.PlatformOcr.DocumentChunk.getDefaultInstance()))
              .setResponseMarshaller(io.grpc.protobuf.lite.ProtoLiteUtils.marshaller(
                  com.deepseek.mobile.platform.v1.PlatformOcr.RecognizedDocument.getDefaultInstance()))
              .build();
        }
      }
    }
    return getRecognizePdfMethod;
  }

  private static volatile io.grpc.MethodDescriptor<com.deepseek.mobile.platform.v1.PlatformOcr.DocumentChunk,
      com.deepseek.mobile.platform.v1.PlatformOcr.RenderedPdfPage> getRenderPdfPageMethod;

  @io.grpc.stub.annotations.RpcMethod(
      fullMethodName = SERVICE_NAME + '/' + "RenderPdfPage",
      requestType = com.deepseek.mobile.platform.v1.PlatformOcr.DocumentChunk.class,
      responseType = com.deepseek.mobile.platform.v1.PlatformOcr.RenderedPdfPage.class,
      methodType = io.grpc.MethodDescriptor.MethodType.CLIENT_STREAMING)
  public static io.grpc.MethodDescriptor<com.deepseek.mobile.platform.v1.PlatformOcr.DocumentChunk,
      com.deepseek.mobile.platform.v1.PlatformOcr.RenderedPdfPage> getRenderPdfPageMethod() {
    io.grpc.MethodDescriptor<com.deepseek.mobile.platform.v1.PlatformOcr.DocumentChunk, com.deepseek.mobile.platform.v1.PlatformOcr.RenderedPdfPage> getRenderPdfPageMethod;
    if ((getRenderPdfPageMethod = PlatformOcrEngineGrpc.getRenderPdfPageMethod) == null) {
      synchronized (PlatformOcrEngineGrpc.class) {
        if ((getRenderPdfPageMethod = PlatformOcrEngineGrpc.getRenderPdfPageMethod) == null) {
          PlatformOcrEngineGrpc.getRenderPdfPageMethod = getRenderPdfPageMethod =
              io.grpc.MethodDescriptor.<com.deepseek.mobile.platform.v1.PlatformOcr.DocumentChunk, com.deepseek.mobile.platform.v1.PlatformOcr.RenderedPdfPage>newBuilder()
              .setType(io.grpc.MethodDescriptor.MethodType.CLIENT_STREAMING)
              .setFullMethodName(generateFullMethodName(SERVICE_NAME, "RenderPdfPage"))
              .setSampledToLocalTracing(true)
              .setRequestMarshaller(io.grpc.protobuf.lite.ProtoLiteUtils.marshaller(
                  com.deepseek.mobile.platform.v1.PlatformOcr.DocumentChunk.getDefaultInstance()))
              .setResponseMarshaller(io.grpc.protobuf.lite.ProtoLiteUtils.marshaller(
                  com.deepseek.mobile.platform.v1.PlatformOcr.RenderedPdfPage.getDefaultInstance()))
              .build();
        }
      }
    }
    return getRenderPdfPageMethod;
  }

  /**
   * Creates a new async stub that supports all call types for the service
   */
  public static PlatformOcrEngineStub newStub(io.grpc.Channel channel) {
    io.grpc.stub.AbstractStub.StubFactory<PlatformOcrEngineStub> factory =
      new io.grpc.stub.AbstractStub.StubFactory<PlatformOcrEngineStub>() {
        @java.lang.Override
        public PlatformOcrEngineStub newStub(io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
          return new PlatformOcrEngineStub(channel, callOptions);
        }
      };
    return PlatformOcrEngineStub.newStub(factory, channel);
  }

  /**
   * Creates a new blocking-style stub that supports all types of calls on the service
   */
  public static PlatformOcrEngineBlockingV2Stub newBlockingV2Stub(
      io.grpc.Channel channel) {
    io.grpc.stub.AbstractStub.StubFactory<PlatformOcrEngineBlockingV2Stub> factory =
      new io.grpc.stub.AbstractStub.StubFactory<PlatformOcrEngineBlockingV2Stub>() {
        @java.lang.Override
        public PlatformOcrEngineBlockingV2Stub newStub(io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
          return new PlatformOcrEngineBlockingV2Stub(channel, callOptions);
        }
      };
    return PlatformOcrEngineBlockingV2Stub.newStub(factory, channel);
  }

  /**
   * Creates a new blocking-style stub that supports unary and streaming output calls on the service
   */
  public static PlatformOcrEngineBlockingStub newBlockingStub(
      io.grpc.Channel channel) {
    io.grpc.stub.AbstractStub.StubFactory<PlatformOcrEngineBlockingStub> factory =
      new io.grpc.stub.AbstractStub.StubFactory<PlatformOcrEngineBlockingStub>() {
        @java.lang.Override
        public PlatformOcrEngineBlockingStub newStub(io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
          return new PlatformOcrEngineBlockingStub(channel, callOptions);
        }
      };
    return PlatformOcrEngineBlockingStub.newStub(factory, channel);
  }

  /**
   * Creates a new ListenableFuture-style stub that supports unary calls on the service
   */
  public static PlatformOcrEngineFutureStub newFutureStub(
      io.grpc.Channel channel) {
    io.grpc.stub.AbstractStub.StubFactory<PlatformOcrEngineFutureStub> factory =
      new io.grpc.stub.AbstractStub.StubFactory<PlatformOcrEngineFutureStub>() {
        @java.lang.Override
        public PlatformOcrEngineFutureStub newStub(io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
          return new PlatformOcrEngineFutureStub(channel, callOptions);
        }
      };
    return PlatformOcrEngineFutureStub.newStub(factory, channel);
  }

  /**
   * <pre>
   * Read-only platform decoding and bundled ML Kit inference. No product paths,
   * credentials, business state, or authority private keys cross this boundary.
   * The Android launcher supplies a per-process random credential over authenticated
   * loopback gRPC. Uploads begin with exactly one header, followed by &lt;=64 KiB data
   * frames. Half-close commits only an exact length and SHA-256 match. The service
   * uses private temporary files and deletes them on completion/cancellation.
   * </pre>
   */
  public interface AsyncService {

    /**
     */
    default io.grpc.stub.StreamObserver<com.deepseek.mobile.platform.v1.PlatformOcr.DocumentChunk> recognizeImage(
        io.grpc.stub.StreamObserver<com.deepseek.mobile.platform.v1.PlatformOcr.RecognizedDocument> responseObserver) {
      return io.grpc.stub.ServerCalls.asyncUnimplementedStreamingCall(getRecognizeImageMethod(), responseObserver);
    }

    /**
     */
    default io.grpc.stub.StreamObserver<com.deepseek.mobile.platform.v1.PlatformOcr.DocumentChunk> recognizePdf(
        io.grpc.stub.StreamObserver<com.deepseek.mobile.platform.v1.PlatformOcr.RecognizedDocument> responseObserver) {
      return io.grpc.stub.ServerCalls.asyncUnimplementedStreamingCall(getRecognizePdfMethod(), responseObserver);
    }

    /**
     */
    default io.grpc.stub.StreamObserver<com.deepseek.mobile.platform.v1.PlatformOcr.DocumentChunk> renderPdfPage(
        io.grpc.stub.StreamObserver<com.deepseek.mobile.platform.v1.PlatformOcr.RenderedPdfPage> responseObserver) {
      return io.grpc.stub.ServerCalls.asyncUnimplementedStreamingCall(getRenderPdfPageMethod(), responseObserver);
    }
  }

  /**
   * Base class for the server implementation of the service PlatformOcrEngine.
   * <pre>
   * Read-only platform decoding and bundled ML Kit inference. No product paths,
   * credentials, business state, or authority private keys cross this boundary.
   * The Android launcher supplies a per-process random credential over authenticated
   * loopback gRPC. Uploads begin with exactly one header, followed by &lt;=64 KiB data
   * frames. Half-close commits only an exact length and SHA-256 match. The service
   * uses private temporary files and deletes them on completion/cancellation.
   * </pre>
   */
  public static abstract class PlatformOcrEngineImplBase
      implements io.grpc.BindableService, AsyncService {

    @java.lang.Override public final io.grpc.ServerServiceDefinition bindService() {
      return PlatformOcrEngineGrpc.bindService(this);
    }
  }

  /**
   * A stub to allow clients to do asynchronous rpc calls to service PlatformOcrEngine.
   * <pre>
   * Read-only platform decoding and bundled ML Kit inference. No product paths,
   * credentials, business state, or authority private keys cross this boundary.
   * The Android launcher supplies a per-process random credential over authenticated
   * loopback gRPC. Uploads begin with exactly one header, followed by &lt;=64 KiB data
   * frames. Half-close commits only an exact length and SHA-256 match. The service
   * uses private temporary files and deletes them on completion/cancellation.
   * </pre>
   */
  public static final class PlatformOcrEngineStub
      extends io.grpc.stub.AbstractAsyncStub<PlatformOcrEngineStub> {
    private PlatformOcrEngineStub(
        io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
      super(channel, callOptions);
    }

    @java.lang.Override
    protected PlatformOcrEngineStub build(
        io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
      return new PlatformOcrEngineStub(channel, callOptions);
    }

    /**
     */
    public io.grpc.stub.StreamObserver<com.deepseek.mobile.platform.v1.PlatformOcr.DocumentChunk> recognizeImage(
        io.grpc.stub.StreamObserver<com.deepseek.mobile.platform.v1.PlatformOcr.RecognizedDocument> responseObserver) {
      return io.grpc.stub.ClientCalls.asyncClientStreamingCall(
          getChannel().newCall(getRecognizeImageMethod(), getCallOptions()), responseObserver);
    }

    /**
     */
    public io.grpc.stub.StreamObserver<com.deepseek.mobile.platform.v1.PlatformOcr.DocumentChunk> recognizePdf(
        io.grpc.stub.StreamObserver<com.deepseek.mobile.platform.v1.PlatformOcr.RecognizedDocument> responseObserver) {
      return io.grpc.stub.ClientCalls.asyncClientStreamingCall(
          getChannel().newCall(getRecognizePdfMethod(), getCallOptions()), responseObserver);
    }

    /**
     */
    public io.grpc.stub.StreamObserver<com.deepseek.mobile.platform.v1.PlatformOcr.DocumentChunk> renderPdfPage(
        io.grpc.stub.StreamObserver<com.deepseek.mobile.platform.v1.PlatformOcr.RenderedPdfPage> responseObserver) {
      return io.grpc.stub.ClientCalls.asyncClientStreamingCall(
          getChannel().newCall(getRenderPdfPageMethod(), getCallOptions()), responseObserver);
    }
  }

  /**
   * A stub to allow clients to do synchronous rpc calls to service PlatformOcrEngine.
   * <pre>
   * Read-only platform decoding and bundled ML Kit inference. No product paths,
   * credentials, business state, or authority private keys cross this boundary.
   * The Android launcher supplies a per-process random credential over authenticated
   * loopback gRPC. Uploads begin with exactly one header, followed by &lt;=64 KiB data
   * frames. Half-close commits only an exact length and SHA-256 match. The service
   * uses private temporary files and deletes them on completion/cancellation.
   * </pre>
   */
  public static final class PlatformOcrEngineBlockingV2Stub
      extends io.grpc.stub.AbstractBlockingStub<PlatformOcrEngineBlockingV2Stub> {
    private PlatformOcrEngineBlockingV2Stub(
        io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
      super(channel, callOptions);
    }

    @java.lang.Override
    protected PlatformOcrEngineBlockingV2Stub build(
        io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
      return new PlatformOcrEngineBlockingV2Stub(channel, callOptions);
    }

    /**
     */
    @io.grpc.ExperimentalApi("https://github.com/grpc/grpc-java/issues/10918")
    public io.grpc.stub.BlockingClientCall<com.deepseek.mobile.platform.v1.PlatformOcr.DocumentChunk, com.deepseek.mobile.platform.v1.PlatformOcr.RecognizedDocument>
        recognizeImage() {
      return io.grpc.stub.ClientCalls.blockingClientStreamingCall(
          getChannel(), getRecognizeImageMethod(), getCallOptions());
    }

    /**
     */
    @io.grpc.ExperimentalApi("https://github.com/grpc/grpc-java/issues/10918")
    public io.grpc.stub.BlockingClientCall<com.deepseek.mobile.platform.v1.PlatformOcr.DocumentChunk, com.deepseek.mobile.platform.v1.PlatformOcr.RecognizedDocument>
        recognizePdf() {
      return io.grpc.stub.ClientCalls.blockingClientStreamingCall(
          getChannel(), getRecognizePdfMethod(), getCallOptions());
    }

    /**
     */
    @io.grpc.ExperimentalApi("https://github.com/grpc/grpc-java/issues/10918")
    public io.grpc.stub.BlockingClientCall<com.deepseek.mobile.platform.v1.PlatformOcr.DocumentChunk, com.deepseek.mobile.platform.v1.PlatformOcr.RenderedPdfPage>
        renderPdfPage() {
      return io.grpc.stub.ClientCalls.blockingClientStreamingCall(
          getChannel(), getRenderPdfPageMethod(), getCallOptions());
    }
  }

  /**
   * A stub to allow clients to do limited synchronous rpc calls to service PlatformOcrEngine.
   * <pre>
   * Read-only platform decoding and bundled ML Kit inference. No product paths,
   * credentials, business state, or authority private keys cross this boundary.
   * The Android launcher supplies a per-process random credential over authenticated
   * loopback gRPC. Uploads begin with exactly one header, followed by &lt;=64 KiB data
   * frames. Half-close commits only an exact length and SHA-256 match. The service
   * uses private temporary files and deletes them on completion/cancellation.
   * </pre>
   */
  public static final class PlatformOcrEngineBlockingStub
      extends io.grpc.stub.AbstractBlockingStub<PlatformOcrEngineBlockingStub> {
    private PlatformOcrEngineBlockingStub(
        io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
      super(channel, callOptions);
    }

    @java.lang.Override
    protected PlatformOcrEngineBlockingStub build(
        io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
      return new PlatformOcrEngineBlockingStub(channel, callOptions);
    }
  }

  /**
   * A stub to allow clients to do ListenableFuture-style rpc calls to service PlatformOcrEngine.
   * <pre>
   * Read-only platform decoding and bundled ML Kit inference. No product paths,
   * credentials, business state, or authority private keys cross this boundary.
   * The Android launcher supplies a per-process random credential over authenticated
   * loopback gRPC. Uploads begin with exactly one header, followed by &lt;=64 KiB data
   * frames. Half-close commits only an exact length and SHA-256 match. The service
   * uses private temporary files and deletes them on completion/cancellation.
   * </pre>
   */
  public static final class PlatformOcrEngineFutureStub
      extends io.grpc.stub.AbstractFutureStub<PlatformOcrEngineFutureStub> {
    private PlatformOcrEngineFutureStub(
        io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
      super(channel, callOptions);
    }

    @java.lang.Override
    protected PlatformOcrEngineFutureStub build(
        io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
      return new PlatformOcrEngineFutureStub(channel, callOptions);
    }
  }

  private static final int METHODID_RECOGNIZE_IMAGE = 0;
  private static final int METHODID_RECOGNIZE_PDF = 1;
  private static final int METHODID_RENDER_PDF_PAGE = 2;

  private static final class MethodHandlers<Req, Resp> implements
      io.grpc.stub.ServerCalls.UnaryMethod<Req, Resp>,
      io.grpc.stub.ServerCalls.ServerStreamingMethod<Req, Resp>,
      io.grpc.stub.ServerCalls.ClientStreamingMethod<Req, Resp>,
      io.grpc.stub.ServerCalls.BidiStreamingMethod<Req, Resp> {
    private final AsyncService serviceImpl;
    private final int methodId;

    MethodHandlers(AsyncService serviceImpl, int methodId) {
      this.serviceImpl = serviceImpl;
      this.methodId = methodId;
    }

    @java.lang.Override
    @java.lang.SuppressWarnings("unchecked")
    public void invoke(Req request, io.grpc.stub.StreamObserver<Resp> responseObserver) {
      switch (methodId) {
        default:
          throw new AssertionError();
      }
    }

    @java.lang.Override
    @java.lang.SuppressWarnings("unchecked")
    public io.grpc.stub.StreamObserver<Req> invoke(
        io.grpc.stub.StreamObserver<Resp> responseObserver) {
      switch (methodId) {
        case METHODID_RECOGNIZE_IMAGE:
          return (io.grpc.stub.StreamObserver<Req>) serviceImpl.recognizeImage(
              (io.grpc.stub.StreamObserver<com.deepseek.mobile.platform.v1.PlatformOcr.RecognizedDocument>) responseObserver);
        case METHODID_RECOGNIZE_PDF:
          return (io.grpc.stub.StreamObserver<Req>) serviceImpl.recognizePdf(
              (io.grpc.stub.StreamObserver<com.deepseek.mobile.platform.v1.PlatformOcr.RecognizedDocument>) responseObserver);
        case METHODID_RENDER_PDF_PAGE:
          return (io.grpc.stub.StreamObserver<Req>) serviceImpl.renderPdfPage(
              (io.grpc.stub.StreamObserver<com.deepseek.mobile.platform.v1.PlatformOcr.RenderedPdfPage>) responseObserver);
        default:
          throw new AssertionError();
      }
    }
  }

  public static final io.grpc.ServerServiceDefinition bindService(AsyncService service) {
    return io.grpc.ServerServiceDefinition.builder(getServiceDescriptor())
        .addMethod(
          getRecognizeImageMethod(),
          io.grpc.stub.ServerCalls.asyncClientStreamingCall(
            new MethodHandlers<
              com.deepseek.mobile.platform.v1.PlatformOcr.DocumentChunk,
              com.deepseek.mobile.platform.v1.PlatformOcr.RecognizedDocument>(
                service, METHODID_RECOGNIZE_IMAGE)))
        .addMethod(
          getRecognizePdfMethod(),
          io.grpc.stub.ServerCalls.asyncClientStreamingCall(
            new MethodHandlers<
              com.deepseek.mobile.platform.v1.PlatformOcr.DocumentChunk,
              com.deepseek.mobile.platform.v1.PlatformOcr.RecognizedDocument>(
                service, METHODID_RECOGNIZE_PDF)))
        .addMethod(
          getRenderPdfPageMethod(),
          io.grpc.stub.ServerCalls.asyncClientStreamingCall(
            new MethodHandlers<
              com.deepseek.mobile.platform.v1.PlatformOcr.DocumentChunk,
              com.deepseek.mobile.platform.v1.PlatformOcr.RenderedPdfPage>(
                service, METHODID_RENDER_PDF_PAGE)))
        .build();
  }

  private static volatile io.grpc.ServiceDescriptor serviceDescriptor;

  public static io.grpc.ServiceDescriptor getServiceDescriptor() {
    io.grpc.ServiceDescriptor result = serviceDescriptor;
    if (result == null) {
      synchronized (PlatformOcrEngineGrpc.class) {
        result = serviceDescriptor;
        if (result == null) {
          serviceDescriptor = result = io.grpc.ServiceDescriptor.newBuilder(SERVICE_NAME)
              .addMethod(getRecognizeImageMethod())
              .addMethod(getRecognizePdfMethod())
              .addMethod(getRenderPdfPageMethod())
              .build();
        }
      }
    }
    return result;
  }
}
