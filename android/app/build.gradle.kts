import java.util.Properties

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
}

// Load local.properties for backend config (not committed to source control).
val localProps = Properties().also { props ->
    val f = rootProject.file("local.properties")
    if (f.exists()) f.inputStream().use(props::load)
}

android {
    namespace = "com.uiblueprint.android"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.uiblueprint.android"
        minSdk = 26
        targetSdk = 34
        versionCode = 1
        versionName = "1.0"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"

        // Inject backend URL + API key from local.properties.
        buildConfigField(
            "String",
            "BACKEND_BASE_URL",
            "\"${localProps.getProperty("BACKEND_BASE_URL", "https://ui-blueprint-backend.onrender.com")}\"",
        )
        buildConfigField(
            "String",
            "BACKEND_API_KEY",
            "\"${localProps.getProperty("BACKEND_API_KEY", "")}\"",
        )
    }

    buildFeatures {
        buildConfig = true
        viewBinding = true
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation(libs.core.ktx)
    implementation(libs.appcompat)
    implementation(libs.material)
    implementation(libs.constraintlayout)
    implementation(libs.work.runtime.ktx)
    implementation(libs.okhttp)
    implementation(libs.recyclerview)
    implementation(libs.lifecycle.viewmodel.ktx)

    testImplementation(libs.junit)
    androidTestImplementation(libs.androidx.junit)
    androidTestImplementation(libs.espresso.core)
}
