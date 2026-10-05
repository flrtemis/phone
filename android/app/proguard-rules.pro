# OkHttp ships its own rules. Nothing else here needs keeping: the app uses no
# reflection, no serialization library, and no JNI.
-dontwarn org.conscrypt.**
-dontwarn org.bouncycastle.**
-dontwarn org.openjsse.**
