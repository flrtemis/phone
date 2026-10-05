// Build with:  cd android && ./gradlew assembleDebug
// (or open this directory in Android Studio)
pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "phone-voice"
include(":app")
